import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize('send,want', [('true', 'true'), ('false', 'false'), (True, 'true'), (False, 'false')])
def test_manual_button_normalizes_only_safe_daily_inputs(monkeypatch, send, want):
    """Manual input cannot enable AI, force delivery, or select another branch."""
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    monkeypatch.setenv('DEFAULT_BRANCH', 'master')
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/master')
    parse = runpy.run_path(str(ROOT / 'scripts/parse_repository_dispatch.py'))['_validated_values']
    event = {'repository': {'full_name': 'owner/repo', 'default_branch': 'master'},
             'inputs': {'send': send}}
    assert parse('daily', event) == {'target_ref': 'refs/heads/master', 'ref_type': 'branch',
                                     'mode': 'off', 'send': want, 'force': 'false'}
    assert parse('resolve', event) is None
    event['inputs']['force'] = True
    assert parse('daily', event) is None
    event['inputs'] = {'send': send}
    monkeypatch.setenv('GITHUB_REF', 'refs/heads/feature')
    assert parse('daily', event) is None


def test_manual_request_authority_still_rejects_other_workflows(monkeypatch):
    """Allow the new request event only with the existing exact identity and SHA checks."""
    env = {'DEFAULT_BRANCH': 'master', 'GITHUB_REF_TYPE': 'branch', 'GITHUB_REF': 'refs/heads/master',
           'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_SHA': 'a' * 40, 'GITHUB_REPOSITORY': 'owner/repo',
           'EXPECTED_TRIGGER_WORKFLOW_NAME': 'AI Daily Request',
           'EXPECTED_TRIGGER_WORKFLOW_PATH': '.github/workflows/request.yml',
           'TRIGGER_WORKFLOW_NAME': 'AI Daily Request', 'TRIGGER_WORKFLOW_PATH': '.github/workflows/request.yml',
           'TRIGGER_WORKFLOW_ID': '123', 'TRIGGER_EVENT': 'workflow_dispatch',
           'TRIGGER_HEAD_BRANCH': 'master', 'TRIGGER_HEAD_SHA': 'a' * 40,
           'TRIGGER_HEAD_REPOSITORY': 'owner/repo', 'TRIGGER_CONCLUSION': 'success'}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    trusted = runpy.run_path(str(ROOT / 'scripts/check_production_authority.py'))['_trusted']
    assert trusted('master')
    monkeypatch.setenv('TRIGGER_HEAD_BRANCH', 'feature')
    assert not trusted('master')
    monkeypatch.setenv('TRIGGER_HEAD_BRANCH', 'master')
    monkeypatch.setenv('EXPECTED_TRIGGER_WORKFLOW_PATH', '.github/workflows/resolve-delivery-request.yml')
    monkeypatch.setenv('TRIGGER_WORKFLOW_PATH', '.github/workflows/resolve-delivery-request.yml')
    assert not trusted('master')
