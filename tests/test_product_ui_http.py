"""Run with pytest: actual loopback HTTP, offline backend, fresh runtime only."""

import re
import socket
import threading
import time

import httpx
import uvicorn
from fastapi.staticfiles import StaticFiles
from test_contract_parser import docx, paragraph

from production_rag.e2e import create_app
from production_rag.product_ui.renderer import HERE, create_renderer


def test_loopback_upload_to_export(tmp_path):
    app = create_app(tmp_path, renderer=create_renderer())
    app.mount("/product-assets", StaticFiles(directory=HERE / "static"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", follow_redirects=True, trust_env=False, timeout=30
        ) as c:
            assert c.get("/").status_code == 200
            r = c.post(
                "/api/contracts",
                files={"file": ("HTTP合同.docx", docx(paragraph("付款期限30自然日。")))},
            ).json()
            selected = c.get(f"/select/{r['task_id']}/{r['version_id']}")
            data = dict(
                re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', selected.text)
            )
            data["status_technical_spec"] = "provided"
            files = {"file_technical_spec": ("technical.txt", "技术要求待核实".encode())}
            response = c.post("/start", data=data, files=files)
            run = str(response.url).split("/")[-1]
            for _ in range(100):
                v = c.get("/export/" + run).json()
                if v.get("overall_status") != "running":
                    break
                time.sleep(0.05)
            assert v["overall_status"] == "completed"
            tid = v["report"]["results"][0]["task_id"]
            assert c.get(f"/evidence/{run}/{tid}").status_code == 200
            assert (
                c.post(
                    f"/opinion/{run}/{tid}",
                    data={
                        "token": data["token"],
                        "reviewer": "HTTP独立复核",
                        "decision": "待复核",
                        "note": "需人工核对证据",
                    },
                ).status_code
                == 200
            )
            assert "HTTP独立复核" in c.get("/print/" + run).text
            assert len(c.get("/export/" + run).json()["human_opinions"]) == 1
            assert str(c.post("/start", data=data, files=files).url).endswith(run)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
