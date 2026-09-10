from pathlib import Path

import yaml


def test_debug_is_manual_confirmed_read_only_and_secret_isolated():
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/world-finance-debug.yml').read_text(encoding='utf-8'))
    assert set(workflow[True]) == {'workflow_dispatch'}
    assert workflow[True]['workflow_dispatch']['inputs']['confirm']['default'] is False
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'secrets.' not in str(workflow['jobs']['verify'])
    job = workflow['jobs']['send']
    assert job['if'] == "github.ref == 'refs/heads/master' && inputs.confirm == true"
    assert job['needs'] == 'verify'
    assert job['environment'] == 'world-finance-production'
    assert 'STATE_TOKEN' not in str(job)
    assert job['steps'][0]['with']['ref'] == '${{ needs.verify.outputs.source_sha }}'
    assert 'world-debug/' == job['steps'][-1]['with']['path']
