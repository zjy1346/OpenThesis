"""Installer jobs share cancellation, never implicitly enable the parser."""
from concurrent.futures import CancelledError
from pathlib import Path
import sys
import threading
from unittest.mock import Mock

import pytest

from openthesis.service import AppService, _ResearchJob


def service(tmp_path):
    app = AppService(tmp_path)
    return app


def test_install_requires_explicit_confirmation_and_existing_python(tmp_path):
    app = service(tmp_path)
    with pytest.raises(ValueError, match='CONFIRMATION_REQUIRED'):
        app.start_structure_install(sys.executable)
    with pytest.raises(ValueError, match='PYTHON_REQUIRED'):
        app.start_structure_install('missing-python.exe',confirmed=True)
    assert not app._jobs


def test_cancelled_installer_cannot_start_duplicate_even_when_stage_changes(tmp_path, monkeypatch):
    app = service(tmp_path)
    active = _ResearchJob('installing',operation_type='structure-install',stage='cancelling',state='cancelling')
    app._jobs[active.job_id] = active
    factory = Mock()
    monkeypatch.setattr(threading, 'Thread', factory)
    result = app.start_structure_install(sys.executable,confirmed=True)
    assert result['job_id'] == active.job_id
    factory.assert_not_called()


@pytest.mark.parametrize('cancelled', [False,True])
def test_completion_does_not_enable_component_or_override_cancellation(tmp_path, monkeypatch, cancelled):
    app = service(tmp_path)
    def install(*args,**kwargs):
        if cancelled:
            next(iter(app._jobs.values())).cancel_event.set()
        return {'available':True,'versions':{},'error_code':''}
    monkeypatch.setattr('openthesis.local_structure.install_component',install)
    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(threading, 'Thread', InlineThread)
    result = app.start_structure_install(sys.executable,confirmed=True)
    assert result['state'] == ('cancelled' if cancelled else 'completed')
    assert app.preferences()['local_structure_enabled'] == 'false'


def test_unavailable_component_cannot_be_enabled(tmp_path):
    app = service(tmp_path)
    with pytest.raises(ValueError):
        app.update_preferences({'local_structure_enabled':'true'})
    assert app.preferences()['local_structure_enabled'] == 'false'
