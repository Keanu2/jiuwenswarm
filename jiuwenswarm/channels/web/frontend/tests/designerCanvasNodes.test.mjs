import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DESIGNER_ADD_TEMPLATES,
  buildManualDesignerNode,
  buildNodeFromLibraryAsset,
  connectNodeToGraph,
  nextTypeIndex,
  offsetCanvasPosition,
  positionRightOfNode,
  removeNodesFromGraph,
  templateForAssetKind,
} from '../node_modules/.cache/designer-canvas-nodes/designerCanvasNodes.js';

test('add templates are image video and audio only', () => {
  assert.deepEqual(
    DESIGNER_ADD_TEMPLATES.map((item) => item.id),
    ['image', 'video', 'audio'],
  );
  assert.deepEqual(
    DESIGNER_ADD_TEMPLATES.map((item) => item.role),
    ['image', 'video', 'audio'],
  );
});

test('buildManualDesignerNode assigns unique id role and centered offset layout', () => {
  const template = DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'image');
  assert.ok(template);
  const first = buildManualDesignerNode({
    template,
    existing: [],
    position: offsetCanvasPosition({ x: 400, y: 300 }, 0),
  });
  assert.equal(first.type, 'image');
  assert.equal(first.config.role, 'image');
  assert.equal(first.config.interaction_mode, 'generate');
  assert.equal(first.label, 'Image 1');
  assert.ok(String(first.id).startsWith('n_image_'));
  const second = buildManualDesignerNode({
    template,
    existing: [first],
    position: offsetCanvasPosition({ x: 400, y: 300 }, 1),
  });
  assert.notEqual(second.id, first.id);
  assert.equal(second.label, 'Image 2');
  assert.equal(second.layout.x, first.layout.x + 28);
});

test('same modality increments the canvas label', () => {
  assert.equal(nextTypeIndex([{ type: 'image' }, { type: 'video' }], 'image'), 2);
});

test('library asset becomes an upload node of matching modality', () => {
  const video = buildNodeFromLibraryAsset({
    asset: {
      id: 'asset_abc',
      filename: 'walk.mp4',
      mime_type: 'video/mp4',
      kind: 'video',
      source: 'uploaded',
      objectUrl: 'blob:walk',
      size: 12,
      created_at: 1,
    },
    existing: [],
    position: { x: 10, y: 20 },
  });
  assert.equal(templateForAssetKind('video').id, 'video');
  assert.equal(video.type, 'video');
  assert.equal(video.config.role, 'video');
  assert.equal(video.config.interaction_mode, 'upload');
  assert.equal(video.config.upload.filename, 'walk.mp4');
  assert.equal(video.output_ref.uri, 'blob:walk');
});

test('positionRightOfNode stacks below an occupied successor slot', () => {
  const source = { id: 'n_brief', layout: { x: 40, y: 100, width: 280, height: 160 } };
  const first = positionRightOfNode(source, []);
  assert.equal(first.x, 40 + 280 + 88);
  assert.equal(first.y, 100);
  const second = positionRightOfNode(source, [
    { id: 'n_other', layout: { x: first.x, y: first.y } },
  ]);
  assert.equal(second.x, first.x);
  assert.equal(second.y, first.y + 160 + 36);
});

test('connectNodeToGraph adds a data edge and predecessor input', () => {
  const template = DESIGNER_ADD_TEMPLATES.find((item) => item.id === 'video');
  assert.ok(template);
  const source = {
    id: 'n_frame_1',
    type: 'image',
    label: 'Keyframe 1',
    config: { role: 'frame' },
    layout: { x: 0, y: 0, width: 280, height: 160 },
  };
  const added = buildManualDesignerNode({
    template,
    existing: [source],
    position: positionRightOfNode(source, [source]),
  });
  const next = connectNodeToGraph({ nodes: [source], edges: [] }, source.id, added);
  assert.ok(next);
  assert.equal(next.nodes.length, 2);
  assert.equal(next.edges.length, 1);
  assert.equal(next.edges[0].source, 'n_frame_1');
  assert.equal(next.edges[0].target, added.id);
  assert.deepEqual(next.nodes[1].config.inputs, ['n_frame_1']);
});

test('removeNodesFromGraph drops edges and strips inputs', () => {
  const graph = {
    nodes: [
      { id: 'n_a', type: 'text', label: 'A', config: { role: 'brief' } },
      { id: 'n_b', type: 'image', label: 'B', config: { role: 'scene', inputs: ['n_a'] } },
      { id: 'n_c', type: 'video', label: 'C', config: { role: 'clip', inputs: ['n_a', 'n_b'] } },
    ],
    edges: [
      { id: 'e_ab', source: 'n_a', target: 'n_b' },
      { id: 'e_bc', source: 'n_b', target: 'n_c' },
    ],
  };
  const next = removeNodesFromGraph(graph, ['n_b']);
  assert.deepEqual(next.nodes.map((node) => node.id), ['n_a', 'n_c']);
  assert.equal(next.edges.length, 0);
  assert.deepEqual(next.nodes[1].config.inputs, ['n_a']);
});
