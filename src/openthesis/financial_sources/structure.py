"""Table-structure normalization; explicit cells, never guessed columns."""
from __future__ import annotations

import html
import json
import math

from ..vision_financials import VisionExtractionResult
from .markdown import collect_markdown_cells


def structure_to_markdown(payload):
    if (not isinstance(payload, dict) or payload.get('schema') != 'financial-structure-v1'
            or not isinstance(payload.get('blocks'), list)):
        raise ValueError('VISION_STRUCTURED_JSON_REQUIRED')
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 4 * 1024 * 1024:
        raise ValueError('VISION_STRUCTURE_SIZE_LIMIT')
    parts = []
    for block in payload['blocks']:
        if not isinstance(block, dict):
            raise ValueError('VISION_STRUCTURE_INVALID')
        bbox = block.get('bbox')
        if bbox is not None and (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in bbox)
                or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
            raise ValueError('VISION_STRUCTURE_GEOMETRY_INVALID')
        if block.get('type') in {'title','text'}:
            parts.append(html.escape(str(block.get('text', ''))))
        elif block.get('type') == 'table':
            if isinstance(block.get('html'), str):
                parts.append(block['html'])
            else:
                rows = block.get('rows')
                if not isinstance(rows, list):
                    raise ValueError('VISION_STRUCTURE_INVALID')
                markup = ['<table>']
                for row in rows:
                    if not isinstance(row, list):
                        raise ValueError('VISION_STRUCTURE_INVALID')
                    markup.append('<tr>')
                    for cell in row:
                        if not isinstance(cell, dict) or not isinstance(cell.get('text'), str):
                            raise ValueError('VISION_STRUCTURE_INVALID')
                        spans = tuple(cell.get(key, 1) for key in ('rowspan','colspan'))
                        if any(type(span) is not int or not 1 <= span <= 32 for span in spans):
                            raise ValueError('VISION_STRUCTURE_SPAN_INVALID')
                        markup.append(f'<td rowspan="{spans[0]}" colspan="{spans[1]}">{html.escape(cell["text"])}</td>')
                    markup.append('</tr>')
                markup.append('</table>')
                parts.append(''.join(markup))
    return '\n'.join(parts)


def parse_structure(payload, company, filing, pages):
    if (not isinstance(payload, dict) or len(pages) != 1
            or type(payload.get('original_page')) is not int
            or payload['original_page'] < 1 or payload['original_page'] != pages[0].original_page):
        return VisionExtractionResult(error_code='VISION_PAGE_MAPPING_REQUIRED')
    try:
        text = structure_to_markdown(payload)
        facts, evidence, error = collect_markdown_cells(text, company, filing, pages[0],
            source_structure=payload)
        return VisionExtractionResult(facts, evidence, (error,) if error else ('VISION_STRUCTURE_CANDIDATES_ONLY',), error)
    except (ValueError, TypeError, OverflowError):
        return VisionExtractionResult(error_code='VISION_STRUCTURE_INVALID')


def paddle_structure(result, original_page):
    """Adapt the pinned PP-StructureV3 pruned result without flattening HTML."""
    if not isinstance(result, dict):
        raise ValueError('VISION_STRUCTURE_INVALID')
    source = result.get('res', result)
    if not isinstance(source, dict):
        raise ValueError('VISION_STRUCTURE_INVALID')
    items = source.get('parsing_res_list', ())
    if not isinstance(items, (list, tuple)):
        raise ValueError('VISION_STRUCTURE_INVALID')
    blocks = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError('VISION_STRUCTURE_INVALID')
        label = item.get('block_label')
        content = item.get('block_content', '')
        if label is not None and not isinstance(label, str):
            raise ValueError('VISION_STRUCTURE_INVALID')
        if not isinstance(content, str):
            raise ValueError('VISION_STRUCTURE_INVALID')
        if label == 'table':
            blocks.append({'type':'table','html':content, 'bbox':item.get('block_bbox')})
        elif label in {'text','paragraph_title','doc_title','figure_table_chart_title'}:
            blocks.append({'type':'title' if 'title' in label else 'text',
                'text':content,'bbox':item.get('block_bbox')})
    return {'schema':'financial-structure-v1','original_page':original_page,
        'backend':'paddle-structure-v3','blocks':blocks,'native_result':result}
