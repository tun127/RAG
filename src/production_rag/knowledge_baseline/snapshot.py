import base64
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import model_validator

from production_rag.review_inputs.models import Frozen, digest

ALGORITHM = "knowledge-sections-parent-document-v1"
POLICIES = {"DP-PROC", "DP-AUTH", "DP-SUP", "DP-TPL", "DP-REV", "DP-PAY", "DP-DEL", "DP-ACC",
            "DP-WAR", "DP-CHG", "DP-EXC", "DP-LIAB", "DP-DISP", "DP-ANN", "DP-ARCH"}
REFERENCES = {"CK01", "CK02", "CK03"}
ORIGIN = "0a300878a7b5998181ae9b2e441fa3079449af56"


def unique(pairs):
    result = {}
    for k, v in pairs:
        if k in result:
            raise ValueError("duplicate JSON key")
        result[k] = v
    return result


def read(root, name):
    relative = Path(name)
    path = root / relative
    resolved = path.resolve()
    if relative.is_absolute() or resolved != path or not resolved.is_relative_to(root):
        raise ValueError("unapproved/redirected knowledge path")
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("source exceeds local experiment limit")
    return raw


class Document(Frozen):
    document_id: str
    version: str
    source_kind: Literal["synthetic_demo_policy", "versioned_public_reference"]
    title: str
    applicability: str
    path: str
    sha256: str
    raw_base64: str
    text: str
    text_sha256: str
    origin_text_start: int = 0
    origin_text_sha256: str
    original_sha256: str
    origin_offset_convention: str = "decoded-utf8-exact"
    dependencies: tuple[str, ...] = ()
    metadata_json: str


class Unit(Frozen):
    unit_id: str
    document_id: str
    version: str
    heading: str
    title_path: tuple[str, ...]
    start: int
    end: int
    text: str

    @property
    def chunk_id(self):
        return self.unit_id


def units_for(document):
    text = document.text
    headings = []
    if document.source_kind == "synthetic_demo_policy":
        for m in re.finditer(r"^(#{1,6}) ([^\n]+)", text, re.M):
            headings.append((m.start(), len(m[1]), m[2].strip()))
    else:
        for m in re.finditer(r"^第[一二三四五六七八九十百千万零〇两]+[编章节条][^\n]*", text, re.M):
            label = re.match(r"第[一二三四五六七八九十百千万零〇两]+([编章节条])", m[0])
            level = {"编": 1, "章": 2, "节": 3, "条": 4}[label[1]]
            title = label[0] if level == 4 else m[0].strip()
            headings.append((m.start(), level, title))
    if not headings or headings[0][0] != 0:
        headings.insert(0, (0, 0, "$preamble"))
    stack = []
    units = []
    for i, (start, level, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        identity = f"{document.document_id}\0{document.version}\0{document.sha256}\0{start}\0{end}"
        units.append(Unit(unit_id=digest(identity.encode()), document_id=document.document_id,
                          version=document.version, heading=title, title_path=tuple(x[1] for x in stack),
                          start=start, end=end, text=text[start:end]))
    return tuple(units)


class Snapshot(Frozen):
    schema_version: Literal["knowledge-snapshot-v1"] = "knowledge-snapshot-v1"
    algorithm: Literal["knowledge-sections-parent-document-v1"] = ALGORITHM
    mode: Literal["full18", "policies15"]
    origin_commit: str
    index_sha256: str
    index_raw_base64: str
    documents: tuple[Document, ...]
    units: tuple[Unit, ...]

    @model_validator(mode="after")
    def verify(self):
        index_raw = base64.b64decode(self.index_raw_base64, validate=True)
        if digest(index_raw) != self.index_sha256:
            raise ValueError("index digest mismatch")
        index = json.loads(index_raw, object_pairs_hook=unique)
        entries = validate_index(index)
        by_id = {e["document_id"]: e for e in entries}
        ids = [d.document_id for d in self.documents]
        expected = POLICIES | REFERENCES if self.mode == "full18" else POLICIES
        if set(ids) != expected or len(ids) != len(expected):
            raise ValueError("snapshot inventory mismatch")
        for d in self.documents:
            e = by_id[d.document_id]
            raw = base64.b64decode(d.raw_base64, validate=True)
            if digest(raw) != d.sha256 or d.sha256 != e["sha256"] or d.version != e["version"] or d.path != e["path"]:
                raise ValueError("source identity/hash drift")
            if d.document_id in POLICIES:
                text = raw.decode("utf-8")
                if d.source_kind != "synthetic_demo_policy" or d.origin_text_start != 0:
                    raise ValueError("policy layer mismatch")
                if d.dependencies != tuple(e["policy_dependencies"]):
                    raise ValueError("dependency drift")
            else:
                reference = json.loads(raw, object_pairs_hook=unique)
                section = reference["sections"][0]
                text = section["text"]
                if (len(reference["sections"]) != 1 or reference["document_id"] != d.document_id
                        or reference["version"] != d.version or reference["source_sha256"] != d.original_sha256
                        or d.original_sha256 != e["source_sha256"] or section["start"] != d.origin_text_start
                        or section["end"] != section["start"] + len(text)
                        or d.source_kind != "versioned_public_reference"):
                    raise ValueError("public reference mapping mismatch")
            if d.text != text or digest(text.encode()) != d.text_sha256:
                raise ValueError("fixed text drift")
        if self.units != tuple(u for d in self.documents for u in units_for(d)):
            raise ValueError("section mapping drift")
        return self

    @property
    def snapshot_id(self):
        return digest(self.model_dump_json().encode())


def validate_index(index):
    policies = index["synthetic_policies"]
    references = index["public_references"]
    if (len(policies) != 15 or {p["document_id"] for p in policies} != POLICIES
            or len(references) != 3 or {r["document_id"] for r in references} != REFERENCES):
        raise ValueError("expected reviewed 15 policies and 3 references")
    for p in policies:
        if (p["path"] != f"synthetic-demo-policy/{p['document_id']}.md"
                or p["classification"] != "synthetic_demo_policy"
                or p["production_status"] != "not_approved"):
            raise ValueError("policy not in approved path/layer")
    for r in references:
        if r["path"] != f"local-runtime/experiment/{r['document_id']}.json" or not r["local_only"]:
            raise ValueError("reference not in approved local path")
    return policies + references


def build(root: Path, *, mode="full18", origin_commit=ORIGIN):
    if mode not in ("full18", "policies15"):
        raise ValueError("explicit snapshot mode required")
    root = root.resolve()
    index_raw = read(root, "experiment-index.json")
    index = json.loads(index_raw, object_pairs_hook=unique)
    validate_index(index)
    provenance = json.loads(read(root, "manifest.json"), object_pairs_hook=unique) if mode == "full18" else None
    documents = []
    for entry in index["synthetic_policies"] + (index["public_references"] if mode == "full18" else []):
        raw = read(root, entry["path"])
        if digest(raw) != entry["sha256"]:
            raise ValueError("source bytes drifted from experiment-index")
        did = entry["document_id"]
        public = did in REFERENCES
        if public:
            metadata = next(d for d in provenance["documents"] if d["document_id"] == did)
            if (metadata["local_original"] != f"local-runtime/{did}.html"
                    or metadata["local_text"] != f"local-runtime/{did}.txt"):
                raise ValueError("original outside reviewed local path")
            original = read(root, metadata["local_original"])
            full_text_bytes = read(root, metadata["local_text"])
            if (digest(original) != entry["source_sha256"] or digest(original) != metadata["sha256"]
                    or digest(full_text_bytes) != metadata["text_sha256"]):
                raise ValueError("reference original/text digest mismatch")
            reference = json.loads(raw, object_pairs_hook=unique)
            section = reference["sections"][0]
            # Upstream experiment JSON was derived with Python universal-newline reading.
            # Preserve that approved text verbatim; never present its offsets as raw HTML/bytes.
            source_text = full_text_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            if source_text[section["start"]:section["end"]] != section["text"]:
                raise ValueError("reference original text span mismatch")
            text = section["text"]
            title = metadata["title"]
            original_sha = entry["source_sha256"]
            original_text_sha = metadata["text_sha256"]
            origin_start = section["start"]
            boundary = entry["scope"] + "；" + metadata["applicable_subjects_and_scenarios"]
        else:
            metadata = entry
            text = raw.decode("utf-8")
            if f"version: {entry['version']}；" not in text:
                raise ValueError("policy declared version mismatch")
            title = text.splitlines()[0].removeprefix("# ")
            original_sha = original_text_sha = digest(raw)
            origin_start = 0
            boundary = "仅虚构企业演示；适用条件、例外和审批以返回完整原文为准，不是法律要求"
        documents.append(Document(
            document_id=did, version=entry["version"], source_kind="versioned_public_reference" if public else "synthetic_demo_policy",
            title=title, applicability=boundary, path=entry["path"], sha256=entry["sha256"], raw_base64=base64.b64encode(raw).decode(),
            text=text, text_sha256=digest(text.encode()), origin_text_start=origin_start,
            origin_text_sha256=original_text_sha, original_sha256=original_sha,
            origin_offset_convention="utf8-universal-newline-derived-text" if public else "decoded-utf8-exact",
            dependencies=tuple(entry.get("policy_dependencies", [])),
            metadata_json=json.dumps({"index_entry": entry, "source_provenance": metadata}, ensure_ascii=False, sort_keys=True),
        ))
    return Snapshot(mode=mode, origin_commit=origin_commit, index_sha256=digest(index_raw),
                    index_raw_base64=base64.b64encode(index_raw).decode(), documents=tuple(documents),
                    units=tuple(u for d in documents for u in units_for(d)))


def save(snapshot, path):
    snapshot = Snapshot.model_validate_json(snapshot.model_dump_json())
    payload = snapshot.model_dump_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump({"sha256": digest(payload.encode()), "payload": payload}, stream, ensure_ascii=False)


def load(path):
    envelope = json.loads(path.read_bytes(), object_pairs_hook=unique)
    if digest(envelope["payload"].encode()) != envelope["sha256"]:
        raise ValueError("snapshot checksum mismatch")
    return Snapshot.model_validate_json(envelope["payload"])
