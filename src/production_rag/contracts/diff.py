"""Exact Unicode code-point edits within current human-approved groups only."""

from collections import Counter
from difflib import SequenceMatcher

from production_rag.contracts.alignment import units
from production_rag.contracts.alignment_store import StaleAlignment


def stream(blocks):
    offset, boundaries = 0, []
    for block in blocks:
        boundaries.append(
            {
                "anchor": block.anchor,
                "start": offset,
                "end": offset + len(block.text),
                "text": block.text,
            }
        )
        offset += len(block.text)
    # No artificial separators: boundaries are metadata, not contract characters.
    return "".join(b.text for b in blocks), boundaries


def pieces(boundaries, start, end):
    if start == end:
        choices = [b for b in boundaries if b["start"] <= start < b["end"]]
        if not choices and boundaries and start == boundaries[-1]["end"]:
            choices = [boundaries[-1]]
        return [
            {"anchor": b["anchor"], "start": start - b["start"], "end": end - b["start"]}
            for b in choices[:1]
        ]
    return [
        {
            "anchor": b["anchor"],
            "start": max(start, b["start"]) - b["start"],
            "end": min(end, b["end"]) - b["start"],
        }
        for b in boundaries
        if max(start, b["start"]) < min(end, b["end"])
    ]


def exact(left, right):
    a, ab = stream(left)
    b, bb = stream(right)
    if max(len(a), len(b)) > 6000:
        return {
            "status": "limit",
            "notice": "本轮逐字比较限每侧 6000 字符；未截断后生成正式差异",
            "standard_blocks": ab,
            "supplier_blocks": bb,
            "edits": [],
        }
    edits = []
    for tag, i, j, k, stop in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        edits.append(
            {
                "kind": tag,
                "standard_start": i,
                "standard_end": j,
                "supplier_start": k,
                "supplier_end": stop,
                "standard_text": a[i:j],
                "supplier_text": b[k:stop],
                "standard_sources": pieces(ab, i, j),
                "supplier_sources": pieces(bb, k, stop),
            }
        )
    assert "".join(e["standard_text"] for e in edits) == a
    assert "".join(e["supplier_text"] for e in edits) == b
    return {"status": "compared", "standard_blocks": ab, "supplier_blocks": bb, "edits": edits}


def differences(data, revision):
    if revision != data["revision"]:
        raise StaleAlignment("人工对应已变化；此修订的差异不可冒充当前结果，请返回对应页面刷新")
    left, right = units(data["standard"]), units(data["supplier"])
    valid = [e for e in data["decisions"] if e["action"] in ("confirm", "correct")]
    usage = Counter(a for e in valid for a in e["supplier_anchors"])
    groups = []
    for event in valid:
        a = [block for anchor, block in left.items() if anchor in event["standard_anchors"]]
        b = [block for anchor, block in right.items() if anchor in event["supplier_anchors"]]
        group = {
            "group_revision": event["revision"],
            "action": event["action"],
            "standard_anchors": [x.anchor for x in a],
            "supplier_anchors": [x.anchor for x in b],
            "overlap_supplier_anchors": [x.anchor for x in b if usage[x.anchor] > 1],
        }
        if any(x.kind == "table" for x in a + b):
            group.update(
                status="table_review",
                notice="整表对应不等于单元格对应；本轮未建立单元格关系，仅展示原表，需人工核对。",
                edits=[],
            )
        else:
            group.update(exact(a, b))
        groups.append(group)
    return {
        "task_id": data["task_id"],
        "standard_version": data["standard_version"],
        "supplier_version": data["supplier_version"],
        "revision": revision,
        "groups": groups,
        "unestablished_standard": [
            a for a in left if not any(a in e["standard_anchors"] for e in valid)
        ],
        "unestablished_supplier": [a for a in right if usage[a] == 0],
        "standard": data["standard"],
        "supplier": data["supplier"],
        "notice": "仅描述有效人工分组内的文字编辑。未建立有效对应不等于新增或删除；位置和块边界变化须人工核对，不判定真实移动。",
    }
