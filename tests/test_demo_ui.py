from production_rag.product_ui.display import build_result
from production_rag.product_ui.renderer import create_renderer


def test_empty_simulated_result_renders():
    context = {'view': {'run_id': 'empty', 'execution_mode': 'SIMULATED', 'report': {'results': []}}}
    view = build_result(context)
    assert view['simulated'] is True
    response = create_renderer()('result', context)
    assert '尚无逐项结果' in response.body.decode()


def test_historical_agent_without_stage_fields_renders():
    context = {'view': {'run_id': 'empty', 'execution_mode': 'SIMULATED', 'report': {'results': [], 'agent_reviews': [{'task_id': 'PAY-01', 'intervened': False}]}}}
    html = create_renderer()('result', context).body.decode()
    assert '查看 Agent 过程' in html
    assert '历史记录未提供' in html
