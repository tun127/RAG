import re

import pytest
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from test_contract_parser import docx, paragraph

from production_rag.e2e import create_app
from production_rag.product_ui.renderer import HERE, create_renderer


@pytest.mark.parametrize(
    "scenario,expected",
    [("normal", "completed"), ("budget_after_one", "partial"), ("crash_after_one", "failed")],
)
def test_product_real_backend(tmp_path, scenario, expected):
    app = create_app(tmp_path, renderer=create_renderer())
    app.mount("/product-assets", StaticFiles(directory=HERE / "static"))
    with TestClient(app) as c:
        assert "尚无合同" in c.get("/").text
        assert c.get("/product-assets/product.js").status_code == 200
        r = c.post(
            "/api/contracts", files={"file": ("合同.docx", docx(paragraph("付款30自然日。")))}
        ).json()
        selected = c.get(f"/select/{r['task_id']}/{r['version_id']}")
        assert selected.status_code == 200
        data = dict(
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', selected.text)
        )
        data["scenario"] = scenario
        response = c.post("/start", data=data)
        assert response.status_code == 200
        run = str(response.url).split("/")[-1]
        assert "审查结果" in response.text and "逐项结果" in response.text
        v = c.get("/export/" + run).json()
        assert v["overall_status"] == expected
        assert c.get("/").status_code == 200
        tid = v["report"]["results"][0]["task_id"]
        evidence = c.get(f"/evidence/{run}/{tid}")
        assert evidence.status_code == 200 and "付款30自然日" in evidence.text
        assert (
            c.post(
                f"/opinion/{run}/{tid}",
                data={
                    "token": data["token"],
                    "decision": "待复核",
                    "reviewer": "<script>复核人</script>",
                    "note": "核实材料 <img src=x>",
                },
            ).status_code
            == 200
        )
        assert "SIMULATED · 流程演练" in response.text
        assert "没有分析你上传的合同" in response.text
        assert "需人工核实任务触发事实" not in response.text
        assert "程序接受 6 / 6" not in response.text
        assert "修改建议" not in response.text
        assert response.text.count('id="review"') == 0
        assert "进入人工复核" in response.text
        assert "<iframe" not in response.text
        review = c.get(f"/review/{run}?item={tid}")
        assert review.status_code == 200
        assert review.text.count('id="review"') == 1
        assert "本事项的意见历史" in review.text
        printed = c.get("/print/" + run)
        assert printed.status_code == 200
        assert "&lt;script&gt;复核人" in printed.text
        assert "<script>复核人" not in printed.text
        assert "核实材料" in printed.text
        assert "合同审查报告" in printed.text
        assert "<form" not in printed.text
        replay = c.post("/start", data=data)
        assert str(replay.url).endswith(run)


def test_status_modes_and_untrusted_context():
    render = create_renderer()
    base = {
        "mode": "LIVE",
        "csrf_token": "token",
        "view": {
            "run_id": "a" * 32,
            "stage": "签署前",
            "policy": "fixed",
            "overall_status": "running",
            "bridge": {
                "parse": {
                    "task_id": "b" * 32,
                    "version_id": "c" * 64,
                    "original_name": "<script>合同</script>",
                    "result": {
                        "coverage_scope": "部分覆盖",
                        "issues": [{"message": "需人工核对附件"}],
                    },
                }
            },
            "report": {
                "status_counts": {"success": 0, "failed": 0, "not_executed": 6},
                "results": [],
            },
            "human_opinions": [],
        },
    }
    page = create_renderer()("run", base).body.decode()
    assert "LIVE ·" in page and 'http-equiv="refresh"' in page
    assert "&lt;script&gt;合同" in page and "<script>合同" not in page
    for status in ("partial", "failed", "state_unconfirmed"):
        base["view"]["overall_status"] = status
        page = render("run", base).body.decode()
        assert 'http-equiv="refresh"' not in page
        assert "需人工核对附件" in page
