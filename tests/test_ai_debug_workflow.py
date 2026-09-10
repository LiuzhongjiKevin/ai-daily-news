from pathlib import Path

import yaml


def test_ai_debug_requires_manual_confirmation_and_cannot_write_production_state():
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/ai-debug.yml').read_text(encoding='utf-8'))
    assert set(workflow[True]) == {'workflow_dispatch'}
    assert workflow[True]['workflow_dispatch']['inputs']['confirm']['default'] is False
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'secrets.' not in str(workflow['jobs']['verify'])
    send = workflow['jobs']['send']
    assert send['needs'] == 'verify'
    assert send['if'] == "github.ref == 'refs/heads/master' && inputs.confirm == true"
    assert send['environment'] == 'ai-daily-production'
    assert 'STATE_TOKEN' not in str(send)
    assert all(step['with']['persist-credentials'] is False
               for step in send['steps'] if 'actions/checkout@' in step.get('uses', ''))
    step = next(step for step in send['steps'] if 'run' in step and 'send_one_off' in step['run'])
    assert step['run'] == 'python scripts/send_one_off_digest.py'
    assert step['env']['MAIL_PROVIDER'] == 'qq'
    assert step['env']['MAIL_TO'] == '${{ secrets.MAIL_TO }}'
