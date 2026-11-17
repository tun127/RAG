"""Explicit upload roles and legacy bridge evidence; never infer from filenames."""

import json
from pathlib import Path


def register(root: Path, task: str, role: str):
    path = root / task / (role + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(
                {"task_id": task, "document_role": role, "source": "explicit_upload_endpoint"},
                stream,
            )
    except FileExistsError:
        pass


def known_roles(runtime):
    roles = {}
    for path in (runtime / "document-roles").glob("*/*.json"):
        record = json.loads(path.read_bytes())
        roles[record["task_id"]] = record["document_role"]
    # Historical runs explicitly bound their contract and uploaded materials already.
    for path in [
        *(runtime / "runs").glob("*/view.json"),
        *(runtime / "jobs").glob("*/pending.json"),
    ]:
        bridge = json.loads(path.read_bytes()).get("bridge", {})
        if bridge.get("parse", {}).get("task_id"):
            roles.setdefault(bridge["parse"]["task_id"], "main_contract")
        for material in bridge.get("material_mapping", []):
            if material.get("parse", {}).get("task_id"):
                roles[material["parse"]["task_id"]] = "material"
    return roles
