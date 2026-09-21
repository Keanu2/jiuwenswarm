import assert from 'node:assert/strict';
import test from 'node:test';

import {
  designerActivityLines,
  designerActivityText,
  isDesignerLeaderNodeId,
} from '../node_modules/.cache/designer-activity/designerActivity.js';

test('leader node id is virtual', () => {
  assert.equal(isDesignerLeaderNodeId('__leader__'), true);
  assert.equal(isDesignerLeaderNodeId('n_brief'), false);
});

test('activity lines prefer tail then latest', () => {
  assert.deepEqual(
    designerActivityLines({
      activity: { kind: 'tool_call', text: 'patch', tool: 'designer_graph_patch' },
      activity_tail: ['reading brief', 'building graph'],
    }),
    ['reading brief', 'building graph'],
  );
  assert.equal(
    designerActivityText({ kind: 'tool_call', text: 'calling patch', tool: 'designer_graph_patch' }),
    'designer_graph_patch · calling patch',
  );
});
