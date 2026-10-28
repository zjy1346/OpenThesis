"""Reusable original-page catalogue, separate from numeric admission."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile


CATALOG_CONTRACT = 'financial-page-catalog-v2'


def document_catalog(context, source_hash, rules_fingerprint, classify, select_candidates=None):
    pages = []
    for number, text in enumerate(context.page_texts, 1):
        useful = sum(character.isalnum() for character in text)
        damaged = text.count('\ufffd')
        kind = 'image_or_empty' if not useful else 'damaged_text' if damaged > useful / 10 else 'native_text'
        statement = classify(text)
        pages.append({'page':number, 'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
            'representation':kind, 'statement':statement[0] if statement else '',
            'scope':statement[1] if statement else '',
            'units':[asdict(item) for item in context.units.declarations if item.page == number],
            'periods':[asdict(item) for item in context.periods if item.page == number]})
    selected = select_candidates(context.page_texts, pages) if callable(select_candidates) else None
    complete = selected is not None
    selected = set(selected or ())
    for page in pages:
        page['candidate'] = page['page'] in selected if complete else None
    return {'contract':CATALOG_CONTRACT, 'source_hash':source_hash,
        'rules_fingerprint':rules_fingerprint, 'candidate_plan_complete':complete, 'pages':pages}


def load_document_catalog(directory, source_hash, rules_fingerprint):
    """Load a source/rule-bound catalog; malformed or legacy entries miss."""
    material = source_hash + ':' + rules_fingerprint
    path = Path(directory) / 'page-catalogs' / (hashlib.sha256(material.encode()).hexdigest() + '.json')
    try:
        catalog = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if (catalog.get('contract') != CATALOG_CONTRACT
            or catalog.get('source_hash') != source_hash
            or catalog.get('rules_fingerprint') != rules_fingerprint
            or not isinstance(catalog.get('pages'), list)
            or type(catalog.get('candidate_plan_complete')) is not bool):
        return None
    for expected, page in enumerate(catalog['pages'], 1):
        if (not isinstance(page, dict) or page.get('page') != expected
                or not isinstance(page.get('text_sha256'), str)
                or (catalog['candidate_plan_complete'] and type(page.get('candidate')) is not bool)):
            return None
    return catalog


def candidate_pages_from_catalog(catalog):
    if not isinstance(catalog, dict) or catalog.get('candidate_plan_complete') is not True:
        return None
    return frozenset(page['page'] for page in catalog['pages'] if page.get('candidate') is True)


def save_document_catalog(directory, catalog):
    """Atomic source/rule bound metadata; never a second page-text pass."""
    directory = Path(directory) / 'page-catalogs'
    directory.mkdir(parents=True, exist_ok=True)
    material = catalog['source_hash'] + ':' + catalog['rules_fingerprint']
    destination = directory / (hashlib.sha256(material.encode()).hexdigest() + '.json')
    data = json.dumps(catalog, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    try:
        if destination.read_text(encoding='utf-8') == data:
            return destination
    except OSError:
        pass
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.tmp', dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
