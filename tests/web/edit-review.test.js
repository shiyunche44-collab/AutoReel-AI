import { describe, expect, it } from 'vitest';
import { clock, matchTag } from '../../app/web/static/js/pages/edit-review.js';

describe('clock', () => {
  it('formats seconds as m:ss.s', () => {
    expect(clock(0)).toBe('0:00.0');
    expect(clock(5.25)).toBe('0:05.3');
    expect(clock(65.3)).toBe('1:05.3');
  });
});

describe('matchTag', () => {
  it('marks fallback cuts first', () => {
    expect(matchTag({ via: 'fallback', need: true, match: 'x' })?.text).toBe('兜底');
  });
  it('returns null when the clause needs no match', () => {
    expect(matchTag({ need: false })).toBeNull();
  });
  it('tells hit from miss', () => {
    expect(matchTag({ need: true, match: '前脸' })?.tone).toBe('tone-success');
    expect(matchTag({ need: true, match: '' })?.tone).toBe('tone-danger');
  });
});
