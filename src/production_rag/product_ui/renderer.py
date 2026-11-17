"""Jinja presentation layer for product pages. No storage, model or write access."""

from pathlib import Path

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import display

HERE = Path(__file__).parent

PAGES = {
    "home": ("home.html", "上传合同", 0),
    "result": ("result.html", "审查结果", 0),
    "review": ("review.html", "人工复核", 0),
    "selection": ("selection.html", "核对文本与材料", 2),
    "run": ("run.html", "审查工作台", 3),
    "evidence": ("evidence.html", "证据原文", 3),
    "report": ("report.html", "审查报告", 5),
    "example": ("example.html", "示例报告", 0),
    "example_report": ("example_report.html", "示例报告打印版", 0),
}


def create_renderer():
    env = Environment(
        loader=FileSystemLoader(HERE / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    def render(page_name, context):
        if page_name == "error":
            view = {
                "status_code": context.get("status_code", 500),
                "message": context.get("message") or "页面读取失败，请返回首页重试。",
            }
            template, title, step = "error.html", "页面暂不可用", 0
            mode = None
            refresh = False
            body_class = ""
        elif page_name == "home":
            view = display.build_home(context)
            template, title, step = PAGES["home"]
            mode = {
                "label": "真实审查已启用" if view["live_enabled"] else "真实审查未启用",
                "tone": "live" if view["live_enabled"] else "sim",
            }
            refresh = False
            body_class = ""
        elif page_name == "selection":
            view = display.build_selection(context)
            template, title, step = PAGES["selection"]
            live = view["live_enabled"]
            mode = {
                "label": "真实审查可用" if live else "仅流程演练",
                "tone": "live" if live else "sim",
            }
            refresh = False
            body_class = ""
        elif page_name == "run":
            view = display.build_run(context)
            template, title, step = PAGES["run"]
            mode = view["mode"]
            refresh = view["status"]["tone"] == "running"
            body_class = ""
        elif page_name == "result":
            view = display.build_result(context)
            template, title, step = PAGES["result"]
            mode = view["mode"]
            refresh = view["status"]["tone"] == "running"
            body_class = ""
        elif page_name == "review":
            view = display.build_review(context)
            template, title, step = PAGES["review"]
            mode = view["mode"]
            refresh = False
            body_class = ""
        elif page_name == "evidence":
            view = display.build_standalone_evidence(context)
            template, title, step = PAGES["evidence"]
            mode = view["mode"]
            refresh = False
            body_class = ""
        elif page_name == "report":
            view = display.build_report(context)
            template, title, step = PAGES["report"]
            mode = view["mode"]
            refresh = False
            body_class = "report-page"
        elif page_name in ("example", "example_report"):
            view = context["example"]
            template, title, step = PAGES[page_name]
            mode = view["mode"]
            refresh = False
            body_class = "report-page example-page"
        else:
            raise ValueError(f"unknown_page:{page_name}")
        saved_report = (context.get('view') or {}).get('report') or {}
        view['agent_reviews'] = saved_report.get('agent_reviews', [])
        view['agent_skips'] = [s for s in saved_report.get('agent_skips', [])
                               if s['reason'] != 'outside_frozen_agent_scope']
        view['review_strategy'] = saved_report.get('review_strategy') or (saved_report.get('config') or {}).get('review_strategy') or context.get('server_strategy')
        integrated = bool((saved_report.get('config') or {}).get('integration_enabled') or context.get('server_strategy'))
        live = not saved_report.get('simulation', True) if saved_report else bool(context.get('live_enabled'))
        view['integration_simulated'] = integrated and not live
        view['integration_live'] = integrated and live
        if any(a.get('first_questioned') and a.get('label') in ('仍有疑点', '复核未完成') for a in view['agent_reviews']):
            if 'summary' in view:
                view['summary']['note'] += ' 首次结果存在尚未解决的质疑，必须人工复核。'
        html = env.get_template(template).render(
            view=view,
            title=title,
            mode=mode,
            step=step,
            refresh=refresh,
            body_class=body_class,
        )
        return HTMLResponse(html)

    return render
