import { useEffect, useMemo, useState } from 'react';
import { designerActivityLines } from '../designerActivity';
import type { DesignerNodeState } from '../executionGraphTypes';

type DesignerActivityPeekProps = {
  state?: Pick<DesignerNodeState, 'activity' | 'activity_tail'> | null;
  testId?: string;
  variant?: 'node' | 'leader';
};

export function DesignerActivityPeek({
  state,
  testId = 'designer-activity-peek',
  variant = 'node',
}: DesignerActivityPeekProps) {
  const lines = useMemo(() => designerActivityLines(state), [state]);
  const [index, setIndex] = useState(0);

  useEffect(() => {
    setIndex(Math.max(0, lines.length - 1));
  }, [lines]);

  useEffect(() => {
    if (lines.length < 2) return undefined;
    const timer = window.setInterval(() => {
      setIndex((current) => (current + 1) % lines.length);
    }, 1400);
    return () => window.clearInterval(timer);
  }, [lines]);

  if (lines.length === 0) return null;
  const current = lines[Math.min(index, lines.length - 1)] || lines[0];
  const previous = lines.length > 1 ? lines[(index - 1 + lines.length) % lines.length] : '';

  return (
    <span
      className={`designer-activity-peek designer-activity-peek--${variant}`}
      data-testid={testId}
      data-variant={variant}
    >
      {previous ? <em>{previous}</em> : null}
      <strong>{current}</strong>
    </span>
  );
}
