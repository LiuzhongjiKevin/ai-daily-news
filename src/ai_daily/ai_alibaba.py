"""AI edition translation through the shared protected finance-branch budget."""
import os
from pathlib import Path


def ledger_directory():
    workspace = os.environ.get('GITHUB_WORKSPACE', '')
    if not workspace or not Path(workspace).is_absolute():
        raise RuntimeError('Translation ledger workspace unavailable')
    return Path(workspace) / '.translation-ledger'


def translate_digest(digest, *, ledger_root=None):
    # Called only after the AI pipeline's durable email reservation / dedup check.
    # Import lazily: world_finance also imports pipeline's timezone helper.
    from ai_daily import world_finance as world
    from ai_daily.state import StateStore
    from ai_daily.world_alibaba import AlibabaTranslator
    from ai_daily.world_translation_budget import make_manifest, reserve

    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REF') != 'refs/heads/master'
            or os.environ.get('WORLD_FINANCE_ENVIRONMENT') != 'ai-daily-production'):
        return digest
    credentials = AlibabaTranslator.credentials()
    if credentials is None:
        return digest
    try:
        root = ledger_root if ledger_root is not None else ledger_directory()
        fields = [text for item in digest.news for text in (item.title, item.summary[:220].strip())]
        fields.extend(item.snapshot.description[:220].strip() for item in digest.repositories)
        manifest = make_manifest(fields)
        if not manifest.characters:
            return digest
        world.verify_budget_history(root)
        grant = reserve(StateStore(root / 'world-state'), manifest, world.translation_now(),
                        os.environ.get('GITHUB_RUN_ID', ''),
                        os.environ.get('GITHUB_RUN_ATTEMPT', ''))
        grant.confirm(world.checkpoint_state(root))
        result = digest.model_copy(deep=True)
        with AlibabaTranslator(*credentials, grant, clock=world.translation_now) as service:
            def summary(text):
                prefix = text[:220].strip()
                translated = service.translate(prefix)
                return text if translated == prefix else translated + ('\u2026' if len(text) > 220 else '')
            for item in result.news:
                item.title = service.translate(item.title)
                item.summary = summary(item.summary)
            for item in result.repositories:
                item.snapshot.description = summary(item.snapshot.description)
            if service.translated:
                result.warnings.append(service.notice)
        return result
    except Exception:  # noqa: BLE001 - no response, credential, git output or exception details
        print('AI Alibaba translation: unavailable; keeping original text.')
        return digest
