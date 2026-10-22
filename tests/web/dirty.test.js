import { describe, expect, it } from 'vitest';
import { countDirty, isDirty } from '../../app/web/static/js/ui/dirty.js';

describe('isDirty()', () => {
  it('没有字段时不算改过', () => {
    expect(isDirty([])).toBe(false);
  });

  it('值都一样时不算改过', () => {
    expect(
      isDirty([
        { now: 'a', was: 'a' },
        { now: 'b', was: 'b' },
      ]),
    ).toBe(false);
  });

  it('有一段改了就算改过', () => {
    expect(
      isDirty([
        { now: 'a', was: 'a' },
        { now: '改过的', was: 'b' },
      ]),
    ).toBe(true);
  });

  it('论点改了也算改过', () => {
    expect(
      isDirty([
        { now: '原文', was: '原文' },
        { now: '新论点', was: '旧论点' },
      ]),
    ).toBe(true);
  });

  it('改回原值就不算改过', () => {
    expect(isDirty([{ now: 'a', was: 'a' }])).toBe(false);
  });
});

describe('countDirty()', () => {
  it('数出改过的条数', () => {
    expect(
      countDirty([
        { now: 'a', was: 'a' },
        { now: 'x', was: 'b' },
        { now: 'c', was: 'c' },
        { now: 'y', was: 'd' },
      ]),
    ).toBe(2);
  });

  it('空数组是 0', () => {
    expect(countDirty([])).toBe(0);
  });

  it('和 isDirty 对得上:有改动时计数大于 0', () => {
    const fields = [
      { now: 'a', was: 'a' },
      { now: 'x', was: 'b' },
    ];
    expect(isDirty(fields)).toBe(true);
    expect(countDirty(fields)).toBeGreaterThan(0);
  });
});
