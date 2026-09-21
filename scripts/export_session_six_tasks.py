"""Version-bound six-task export for accepted Product Sessions; no model calls."""
from __future__ import annotations

import asyncio
from collections import Counter
import copy
import functools
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import quote

from scripts.export_trajectory_dataset import apply_patches, make_patches
from src.orchestration.browser_evidence import collect_browser_evidence
from src.orchestration.webcompass_protocol import (
    EDIT_TYPES, REPAIR_TYPES, IMAGE_GENERATION_PROMPT, TEXT_GENERATION_PROMPT,
    construct_edit_text, construct_repair_text, image_generation_document,
    markdown_repository, official_patches, search_replace_xml,
)

TASKS = ('text-generation', 'image-generation', 'text-editing', 'image-editing', 'text-repair', 'image-repair')
POLICY = 'session-six-v3'


def read(path, default=None):
    return json.loads(path.read_text()) if path.is_file() else default


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def compound_edits(records):
    """All contiguous 4..12 windows, preserving their chronological instructions."""
    edits = [r for r in records if r['task'] == 'text-editing']
    result = []
    for end in range(4, len(edits) + 1):
        for length in range(4, min(12, end) + 1):
            window = edits[end-length:end]
            for before, after in zip(window, window[1:]):
                if before['reference']['dst_code'] != after['instruction']['src_code']:
                    raise ValueError('non-contiguous Edit window')
            record = copy.deepcopy(window[-1])
            record['instance_id'] = window[0]['instance_id'] + '__through__' + window[-1]['trajectory']['edit_id']
            descriptions = [dict(d) for r in window for d in r['quality']['task_descriptions']]
            record['task_type'] = [d['task_type'] for d in descriptions]
            record['description'] = '\n'.join(d['description'] for d in descriptions)
            record['instruction'] = copy.deepcopy(window[0]['instruction'])
            record['instruction']['description'] = record['description']
            record['label_modified_files'] = make_patches(record['instruction']['src_code'], record['reference']['dst_code'], record['task_type'][0])
            record['trajectory'].update(source_commit=window[0]['trajectory']['source_commit'],
                source_project=window[0]['source_project'],
                source_edit_id=window[0]['trajectory']['edit_id'],
                included_edit_ids=[r['trajectory']['edit_id'] for r in window])
            record['quality'].update(edit_kind='compound_edit', task_descriptions=descriptions,
                                     task_count=length, trajectory_role='contiguous_edit_window')
            if record['label_modified_files']:
                result.append(record)
    return result


def capture_flows(record):
    """Reuse existing state setup, ending before outcomes; no new semantic tests."""
    harness = Path(record['source_project']).parent / '.harness'
    if record['task'] == 'text-repair':
        failed_round = int(record['quality']['source_checkpoint_id'].split('@')[0].split('_')[-1])
        packet = read(harness / f'repair_packet_round_{failed_round}.json', {})
        ids = {c['check_id'] for c in packet.get('failed_checks', [])}
        frozen = read(harness / 'hidden_oracle_checks.json', {})
        hidden = frozen if isinstance(frozen, list) else frozen.get('checks', [])
        visible = (read(harness / 'atomic_edit_plan.json', {}) or {}).get('checks', [])
        checks = [c for c in [*visible, *hidden] if c.get('id') in ids]
    else:
        checks = (read(harness / 'atomic_edit_plan.json', {}) or {}).get('checks', [])
    flows, seen = [], set()
    for check in checks:
        setup = []
        for action in check.get('actions', []):
            kind = action.get('action', '')
            if kind.startswith('assert_'):
                # Capture the state just reached. Subsequent outcomes are not
                # required to pass on a defective Repair source.
                key = digest([check.get('route', '/'), setup])
                if setup and key not in seen:
                    seen.add(key)
                    flows.append({'route': check.get('route', '/'), 'actions': copy.deepcopy(setup),
                                  'description': check.get('task') or 'Observed interaction state'})
            else:
                setup.append(action)
    return flows


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


async def capture_version(frontend, commit, code, flows, output):
    """Cache all HTML pages and required interaction states from an exact Git tree."""
    tree = subprocess.check_output(['git', 'rev-parse', commit + '^{tree}'], cwd=frontend, text=True).strip()
    identity = {'policy': POLICY, 'tree': tree, 'code': digest(code), 'flows': flows}
    key = digest(identity)
    directory = output / 'images' / key
    manifest_path = directory / 'manifest.json'
    saved = read(manifest_path)
    if saved and saved.get('identity') == identity and all(
        (output / image['path']).is_file() and hashlib.sha256((output / image['path']).read_bytes()).hexdigest() == image['sha256']
        for image in saved.get('images', [])) and saved.get('images') and all(
        (output / asset['asset']).is_file() and hashlib.sha256((output / asset['asset']).read_bytes()).hexdigest() == asset['sha256']
        for asset in saved.get('resources', [])):
        return saved
    directory.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='session-export-') as temp:
        root = Path(temp).resolve()
        archive = subprocess.check_output(['git', 'archive', commit], cwd=frontend)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar.getmembers():
                path = root / member.name
                if member.issym() or member.islnk() or not path.resolve().is_relative_to(root):
                    raise ValueError('unsafe resource in source snapshot')
                if member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(tar.extractfile(member).read())
        for file in code:
            path = root / file['path']
            if not path.resolve().is_relative_to(root) or not path.is_file() or path.read_text() != file['code']:
                raise ValueError('screenshot source does not match exported code')
        pages = [{'path': f['path'], 'route': '/' + quote(f['path']),
                  'role': 'entry' if f['path'] == 'index.html' else 'page'}
                 for f in code if f['path'].lower().endswith(('.html', '.htm'))]
        if not pages:
            raise ValueError('static Session export needs an HTML entry')
        server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(root)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        scenes = [{'route': p['route'], 'actions': [], 'description': 'Initial page', 'page': p['path']} for p in pages]
        scenes += [{**f, 'page': f['route'].lstrip('/') or 'index.html'} for f in flows]
        checks = [{'id': f'capture-{i}', 'route': scene['route'],
                   'actions': [*scene['actions'], {'action': 'assert_visible', 'selector': 'body'}]}
                  for i, scene in enumerate(scenes)]
        try:
            evidence = await asyncio.wait_for(collect_browser_evidence(
                app_url=f'http://127.0.0.1:{server.server_port}', checks=checks,
                output_path=directory / 'capture.json', headless=True,
                capture_screenshots=True, screenshot_full_page=True, action_timeout_ms=1500), timeout=180)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
        images = []
        for scene, result in zip(scenes, evidence['checks'], strict=True):
            name = result.get('screenshot')
            if not name:
                raise ValueError('image capture failed: ' + str(result.get('navigation_error') or result.get('status')))
            path = directory / name
            images.append({'path': path.relative_to(output).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'route': scene['route'], 'page': scene['page'], 'state': 'interaction' if scene['actions'] else 'initial',
                'actions': scene['actions'], 'description': scene['description'],
                'setup_completed': bool(result.get('steps') and result['steps'][-1].get('action') == 'assert_visible' and all(s.get('ok') for s in result['steps'])),
                'capture_status': result['status']})
        # Binary resources stay available to the consumer, bound to the same tree.
        resources = []
        code_paths = {f['path'] for f in code}
        for path in sorted(root.rglob('*')):
            if path.is_file() and path.relative_to(root).as_posix() not in code_paths:
                relative = path.relative_to(root).as_posix()
                destination = output / 'resources' / tree / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                original_bytes = path.read_bytes()
                if not destination.is_file() or destination.read_bytes() != original_bytes:
                    destination.write_bytes(original_bytes)
                resources.append({'path': relative, 'asset': destination.relative_to(output).as_posix(),
                                  'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {'identity': identity, 'pages': pages, 'images': images, 'resources': resources}
    atomic_json(manifest_path, manifest)
    return manifest


def has_visual_change(source, target, output):
    from PIL import Image, ImageChops
    for a, b in zip(source['images'], target['images']):
        if (a['route'], a['actions']) != (b['route'], b['actions']):
            continue
        with Image.open(output/a['path']) as left, Image.open(output/b['path']) as right:
            if left.size != right.size:
                return True
            diff = ImageChops.difference(left.convert('RGB'), right.convert('RGB'))
            changed = sum(max(pixel) > 16 for pixel in diff.getdata())
            if changed / (left.width * left.height) > 0.0005:
                return True
    return False


def build_row(record, task, source, target, output):
    generation = task.endswith('generation')
    repair = task.endswith('repair')
    image = task.startswith('image-')
    descriptions = record['quality'].get('repair_task_descriptions' if repair else 'task_descriptions', [])
    source_code, target_code = record['instruction']['src_code'], record['reference']['dst_code']
    if not generation and apply_patches(source_code, record['label_modified_files']) != target_code:
        raise ValueError('patch replay does not equal the exported target')
    public = {'src_code': source_code, 'description': descriptions}
    if generation:
        prompt = (IMAGE_GENERATION_PROMPT.format(document=image_generation_document(record['instance_id'], [i['path'] for i in target['images']]))
                  if image else TEXT_GENERATION_PROMPT.format(document=record['description']))
        if image and not any(f['path'].lower() == 'readme.md' for f in target_code):
            prompt = prompt.replace('- MUST include a README.md with the simplest way to run locally.\n', '')
        answer = markdown_repository(target_code)
    else:
        prompt = construct_repair_text(public) if repair else construct_edit_text(public)
        if not repair and record['quality'].get('edit_kind') == 'compound_edit':
            prompt += '\nApply the listed edits in order; later steps may use capabilities added by earlier steps.\n'
        answer = search_replace_xml(record['label_modified_files'])
    picture_mapping = []
    if image:
        groups = [('target', target)] if generation else [('current', source), ('target', target)] if repair else [('current', source)]
        for role, bundle in groups:
            for entry in bundle['images']:
                picture_mapping.append({**entry, 'role': role})
        prompt += '\nReference image order:\n' + '\n'.join(
            f"<image>\nImage {i+1}: {item['role']}, page {item['route']}, {item['state']} state."
            for i, item in enumerate(picture_mapping))
    types = [d['task_type'] for d in descriptions]
    allowed = REPAIR_TYPES if repair else EDIT_TYPES
    count_aligned = not generation and 4 <= len(descriptions) <= 12
    taxonomy_aligned = not generation and bool(types) and all(t in allowed for t in types)
    page_mode = 'mp' if len((source or target)['pages']) > 1 else 'sp'
    return {'schema_version': POLICY, 'instance_id': record['instance_id'] + '__' + task,
        'task': task, 'instruction': prompt, 'input_files': [] if generation else source_code,
        'input_images': [i['path'] for i in picture_mapping], 'images': [i['path'] for i in picture_mapping],
        'messages': [{'role': 'user', 'content': prompt}, {'role': 'assistant', 'content': answer}],
        'response': target_code if generation else official_patches(record['label_modified_files']),
        'metadata': {'native_instance_id': record['instance_id'], 'lineage_group': record['trajectory'].get('session_id'),
            'trajectory': record['trajectory'], 'task_count': len(descriptions), 'task_types': types,
            'task_descriptions': descriptions, 'page_mode': page_mode,
            'count_aligned': count_aligned, 'taxonomy_aligned': taxonomy_aligned,
            'webcompass_aligned': count_aligned and taxonomy_aligned,
            'page_manifest': {'source': source['pages'] if source else [], 'target': target['pages']},
            'image_mapping': picture_mapping, 'source_hash': digest(source_code), 'target_hash': digest(target_code),
            'resources': (target if generation else source)['resources'],
            'repair_origin': record['quality'].get('repair_origin')}}


def sampling_manifest(rows, distribution):
    strata = {}
    for task, records in rows.items():
        if task.endswith('generation'):
            continue
        for mode in ('sp', 'mp'):
            eligible = [r for r in records if r['metadata']['count_aligned'] and r['metadata']['page_mode'] == mode]
            counts = Counter(r['metadata']['task_count'] for r in eligible)
            family = 'repair' if task.endswith('repair') else 'editing'
            target = distribution['counts'][family][mode]
            total = sum(target.values())
            strata[task + '/' + mode] = {
                'target_probabilities': {n: c/total for n, c in target.items()},
                'page_mode_probability': 0.5,
                'available_counts': dict(sorted(counts.items())),
                'missing_counts': [int(n) for n,c in target.items() if c and not counts[int(n)]],
                'samples': [{'instance_id': r['instance_id'], 'task_count': r['metadata']['task_count'], 'weight': 0.5 * target[str(r['metadata']['task_count'])]/total/counts[r['metadata']['task_count']]} for r in eligible]}
    return {'policy': 'preserve_all_weight_aligned_view', 'distribution_source': distribution,
            'split_group': 'lineage_group', 'strata': strata}


async def export_six_tasks(records, session, output, distribution_path):
    output.mkdir(parents=True, exist_ok=True)
    rows = {task: [] for task in TASKS}
    skips = []
    cache = {}
    first_edit = next((r for r in records if r['task'] == 'text-editing'), None)
    lineage_group = digest(first_edit['instruction']['src_code']) if first_edit else session['session_id']
    expanded = [*records, *compound_edits(records)]
    for record in expanded:
        frontend = Path(record['source_project'])
        source_frontend = Path(record['trajectory'].get('source_project') or record['source_project'])
        flows = capture_flows(record)
        async def capture(root, commit, code, states):
            key = digest([str(root), commit, states])
            if key not in cache:
                cache[key] = await capture_version(root, commit, code, states, output)
            return cache[key]
        target = await capture(frontend, record['trajectory']['destination_commit'], record['reference']['dst_code'], flows)
        source = None
        if record['task'] != 'text-generation':
            source = await capture(source_frontend, record['trajectory']['source_commit'], record['instruction']['src_code'], flows if record['task']=='text-repair' else [])
        for task in (record['task'], record['task'].replace('text-', 'image-', 1)):
            if task == 'image-repair' and not has_visual_change(source, target, output):
                skips.append({'instance_id': record['instance_id'], 'task': task, 'reason': 'no_visible_repair_difference'})
                continue
            row = build_row(record, task, source, target, output)
            row['metadata']['lineage_group'] = lineage_group
            rows[task].append(row)
    manifest = {'schema_version': POLICY, 'session_id': session['session_id'], 'lineage_group': lineage_group, 'asset_base': '.', 'counts': {t: len(r) for t,r in rows.items()},
                'image_skips': skips, 'records': {}}
    for task, values in rows.items():
        content = ''.join(json.dumps(v, ensure_ascii=False)+'\n' for v in values)
        path = output / task / ('records-' + hashlib.sha256(content.encode()).hexdigest()[:16] + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.jsonl.tmp')
        temp.write_text(content)
        temp.replace(path)
        manifest['records'][task] = {'path': path.relative_to(output).as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    distribution = read(distribution_path)
    if distribution is None:
        raise ValueError('missing verified WebCompass subtask distribution')
    sampling = sampling_manifest(rows, distribution)
    sampling_path = 'sampling-' + digest(sampling)[:16] + '.json'
    atomic_json(output/sampling_path, sampling)
    manifest['sampling_manifest'] = sampling_path
    atomic_json(output/'dataset_index.json', manifest)
    return {'status': 'completed', 'output': str(output), **manifest}


def merge_batch_indexes(outputs, destination):
    """Reweight the pooled batch; keep every immutable shard and lineage group."""
    batches = []
    strata = {}
    seen_ids = set()
    for output in outputs:
        index = read(output/'dataset_index.json')
        if not index:
            continue
        samples = read(output/index['sampling_manifest'])
        batches.append({'session_id': index['session_id'], 'root': str(output.resolve()),
                        'index': str((output/'dataset_index.json').resolve()), 'counts': index['counts']})
        for key, value in samples['strata'].items():
            group = strata.setdefault(key, {'target_probabilities': value['target_probabilities'],
                                           'page_mode_probability': 0.5, 'samples': []})
            if group['target_probabilities'] != value['target_probabilities']:
                raise ValueError('inconsistent benchmark distribution versions')
            for sample in value['samples']:
                identity = (key, sample['instance_id'])
                if identity in seen_ids:
                    raise ValueError('duplicate instance across Session exports')
                seen_ids.add(identity)
                group['samples'].append({**sample, 'lineage_group': index.get('lineage_group', index['session_id']),
                                         'dataset_root': str(output.resolve())})
    for group in strata.values():
        counts = Counter(sample['task_count'] for sample in group['samples'])
        group['available_counts'] = dict(sorted(counts.items()))
        group['missing_counts'] = [int(n) for n,p in group['target_probabilities'].items() if p and not counts[int(n)]]
        for sample in group['samples']:
            n = sample['task_count']
            sample['weight'] = 0.5 * group['target_probabilities'][str(n)] / counts[n]
    result = {'policy': POLICY, 'sessions': batches, 'strata': strata,
              'counts': {task: sum(b['counts'][task] for b in batches) for task in TASKS},
              'split_group': 'lineage_group'}
    atomic_json(destination, result)
    return result
