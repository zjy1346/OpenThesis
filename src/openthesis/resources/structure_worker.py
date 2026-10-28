"""Standalone optional runtime entry. Imports no application or user database.

Model downloads are permitted ONLY by the explicit prepare command. Parsing
passes pinned local directories for every enabled module. No training and no
document-network inference are implemented.
"""
import argparse
import importlib.metadata
import json
import hashlib
import os
import time
from pathlib import Path

MODELS = {
    'layout_detection': 'PP-DocLayout_plus-L',
    'text_detection': 'PP-OCRv5_server_det',
    'text_recognition': 'PP-OCRv5_server_rec',
    'textline_orientation': 'PP-LCNet_x1_0_textline_ori',
    'table_classification': 'PP-LCNet_x1_0_table_cls',
    'wired_table_structure_recognition': 'SLANeXt_wired',
    'wireless_table_structure_recognition': 'SLANet_plus',
    'wired_table_cells_detection': 'RT-DETR-L_wired_table_cell_det',
    'wireless_table_cells_detection': 'RT-DETR-L_wireless_table_cell_det',
    'table_orientation_classify': 'PP-LCNet_x1_0_doc_ori',
}
VERSIONS = {'paddleocr':'3.7.0', 'paddlex':'3.7.2', 'paddlepaddle':'3.2.0'}


def write_result(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def peak_memory_bytes():
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('size',wintypes.DWORD), ('faults',wintypes.DWORD)] + [
            (name,ctypes.c_size_t) for name in ('peak_ws','ws','peak_pool','pool','peak_nonpool',
                'nonpool','pagefile','peak_pagefile')]
    kernel, api = ctypes.windll.kernel32, ctypes.windll.psapi
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    api.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    counters = Counters(size=ctypes.sizeof(Counters))
    if api.GetProcessMemoryInfo(kernel.GetCurrentProcess(),ctypes.byref(counters),ctypes.sizeof(counters)):
        return counters.peak_ws
    return None


def parse_pages(pipeline, input_path, jobs, versions, *, progress_path=None, model_load_seconds=0):
    """One PDF and model session, durable independent page artifacts."""
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(input_path)
    try:
        for job in jobs:
            number, destination = int(job['page']), Path(job['output'])
            if progress_path:
                write_result(progress_path, {'page': number, 'state': 'running'})
            started = time.monotonic()
            try:
                if not 1 <= number <= len(document):
                    raise ValueError('STRUCTURE_PAGE_OUT_OF_RANGE')
                destination.parent.mkdir(parents=True, exist_ok=True)
                page = document[number - 1]
                try:
                    bitmap = page.render(scale=2)
                    try:
                        bitmap.to_pil().save(destination.with_suffix('.png'))
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                results = [item.json for item in pipeline.predict(input=str(destination.with_suffix('.png')))]
                write_result(destination, {'versions': versions, 'results': results,
                    'metrics': {'model_load_seconds': model_load_seconds,
                        'inference_seconds': round(time.monotonic() - started, 4),
                        'peak_memory_bytes':peak_memory_bytes()}})
                if progress_path:
                    write_result(progress_path, {'page': number, 'state': 'ready'})
            except Exception as exc:
                code = str(exc) if str(exc).startswith('STRUCTURE_') else 'STRUCTURE_PAGE_FAILED'
                write_result(destination.with_suffix('.error.json'), {'page': number, 'error_code': code})
                if progress_path:
                    write_result(progress_path, {'page': number, 'state': 'failed'})
    finally:
        document.close()


def locate_pages(input_path, output):
    """Cheap image layout ranking before loading OCR; no page is discarded."""
    import pypdfium2 as pdfium
    document = pdfium.PdfDocument(input_path)
    pages = []
    try:
        for number in range(len(document)):
            page = document[number]
            try:
                bitmap = page.render(scale=.35)
                try:
                    image = bitmap.to_pil().convert('L')
                    image.thumbnail((280, 400))
                    width, height = image.size
                    pixels = list(image.getdata())
                    densities = [sum(value < 160 for value in pixels[row * width:(row + 1) * width]) / width
                        for row in range(height)]
                    rules = sum(value > .5 for value in densities)
                    text_rows = sum(.015 < value < .5 for value in densities)
                    pages.append({'page':number + 1, 'layout_score':rules * 10 + text_rows,
                        'table_rule_rows':rules, 'text_rows':text_rows})
                finally:
                    bitmap.close()
            finally:
                page.close()
    finally:
        document.close()
    write_result(output, {'pages':sorted(pages, key=lambda item:(-item['layout_score'], item['page'])),
        'strategy':'low-resolution-layout-ranking-v1'})


def prepare_models(directory):
    """Fetch exact official revisions, verify BEFORE any model is executed."""
    lock = json.loads(Path(__file__).with_name('structure-models.lock.json').read_text(encoding='utf-8'))
    if lock['versions'] != VERSIONS or {item['name'] for item in lock['models']} != set(MODELS.values()):
        raise ValueError('STRUCTURE_MODEL_LOCK_INVALID')
    for model in lock['models']:
        destination = directory / model['name']
        files = [item for item in lock['files'] if item['path'].split('/')[0] == model['name']]
        def matches(item):
            path = directory / item['path']
            if not path.is_file() or path.stat().st_size != item['size']:
                return False
            with path.open('rb') as handle:
                return hashlib.file_digest(handle, 'sha256').hexdigest() == item['sha256']
        if not all(matches(item) for item in files):
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id='PaddlePaddle/' + model['name'], revision=model['revision'],
                allow_patterns=[Path(item['path']).name for item in files], local_dir=destination)
        if not files or not all(matches(item) for item in files):
            raise ValueError('STRUCTURE_MODEL_INTEGRITY_FAILED')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--models', required=True)
    parser.add_argument('--input')
    parser.add_argument('--page', type=int)
    parser.add_argument('--batch')
    parser.add_argument('--locate', action='store_true')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if args.locate:
        if not args.input:
            raise ValueError('STRUCTURE_SOURCE_UNAVAILABLE')
        locate_pages(args.input, args.output)
        return
    if not args.prepare and (not args.input or (not args.batch and (args.page is None or args.page < 1))):
        raise ValueError('STRUCTURE_PAGE_REQUIRED')
    if not args.prepare:
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
    versions = {name: importlib.metadata.version(name) for name in VERSIONS}
    if versions != VERSIONS:
        raise ValueError('STRUCTURE_RUNTIME_VERSION_MISMATCH')
    if args.prepare:
        prepare_models(Path(args.models))
    parameters = dict(device='cpu', cpu_threads=4, use_doc_orientation_classify=False,
        use_doc_unwarping=False, use_textline_orientation=False, use_seal_recognition=False,
        use_formula_recognition=False, use_chart_recognition=False,
        use_region_detection=False, use_table_recognition=True)
    for role, name in MODELS.items():
        parameters[role + '_model_name'] = name
        directory = Path(args.models) / name
        if not directory.is_dir() or not (directory / 'inference.yml').is_file():
            raise ValueError('STRUCTURE_MODEL_NOT_INSTALLED')
        parameters[role + '_model_dir'] = str(directory)
    from paddleocr import PPStructureV3
    print(json.dumps({'phase':'models'}), flush=True)
    load_started = time.monotonic()
    pipeline = PPStructureV3(**parameters)
    model_load_seconds = round(time.monotonic() - load_started, 4)
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.prepare:
        from PIL import Image, ImageDraw
        image = Image.new('RGB', (640, 320), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((20, 20), 'Consolidated balance sheet - USD', fill='black')
        for x in (20, 230, 420, 620):
            draw.line((x,60,x,270), fill='black',width=2)
        for y in (60,110,160,210,270):
            draw.line((20,y,620,y),fill='black',width=2)
        for y, texts in ((75,('Item','2025','2024')), (125,('Assets','100','90')),
                         (175,('Liabilities','60','50')), (225,('Equity','40','40'))):
            for x, text in zip((30,250,440), texts):
                draw.text((x,y),text,fill='black')
        image_path = destination.with_suffix('.png')
        image.save(image_path)
        results = [item.json for item in pipeline.predict(input=str(image_path))]
        if not results:
            raise ValueError('STRUCTURE_CPU_INFERENCE_FAILED')
        destination.write_text(json.dumps({'versions':versions,'models':MODELS,
            'cpu_inference_verified':True, 'result_count':len(results)}), encoding='utf-8')
    else:
        print(json.dumps({'phase':'parsing'}), flush=True)
        jobs = json.loads(Path(args.batch).read_text(encoding='utf-8')) if args.batch else [
            {'page': args.page, 'output': str(destination)}]
        if not isinstance(jobs, list) or not jobs:
            raise ValueError('STRUCTURE_BATCH_INVALID')
        try:
            parse_pages(pipeline, args.input, jobs, versions,
                progress_path=destination if args.batch else None, model_load_seconds=model_load_seconds)
        finally:
            pipeline.close()
        print(json.dumps({'phase':'completed'}), flush=True)
        return
    print(json.dumps({'phase':'completed'}), flush=True)
    pipeline.close()


if __name__ == '__main__':
    main()
