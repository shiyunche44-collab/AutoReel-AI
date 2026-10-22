import { describe, expect, it } from 'vitest';
import { LIVE_STATUSES, matchFact, matchProject, matchShot } from '../../app/web/static/js/ui/filters.js';

describe('matchShot()', () => {
  it('all 显示全部', () => {
    for (const st of ['pending', 'running', 'failed', '']) {
      expect(matchShot(st, 'all')).toBe(true);
    }
  });

  it('live 只显示还在跑的状态', () => {
    for (const st of LIVE_STATUSES) expect(matchShot(st, 'live')).toBe(true);
    for (const st of ['pending', 'failed', 'succeeded', '']) expect(matchShot(st, 'live')).toBe(false);
  });

  it('具体状态只显示那一种', () => {
    expect(matchShot('failed', 'failed')).toBe(true);
    expect(matchShot('succeeded', 'failed')).toBe(false);
  });

  it('状态是空串时不会被当成进行中', () => {
    expect(matchShot('', 'live')).toBe(false);
  });
});

describe('matchFact()', () => {
  it('范围是 all 时不挑范围', () => {
    expect(matchFact('official', '续航 835 公里', 'all', '')).toBe(true);
  });

  it('范围要对上', () => {
    expect(matchFact('official', '续航', 'official', '')).toBe(true);
    expect(matchFact('official', '续航', 'media', '')).toBe(false);
  });

  it('搜索词要命中(文本已转小写)', () => {
    expect(matchFact('official', '835 公里 续航', 'all', '835')).toBe(true);
    expect(matchFact('official', '835 公里 续航', 'all', '电池')).toBe(false);
  });

  it('范围和搜索词要同时满足', () => {
    expect(matchFact('official', '835 公里', 'media', '835')).toBe(false);
    expect(matchFact('official', '835 公里', 'official', '835')).toBe(true);
  });
});

describe('matchProject()', () => {
  it('分组是 all 时不挑分组', () => {
    expect(matchProject('gate', '小米yu7', 'all', '')).toBe(true);
  });

  it('分组要对上', () => {
    expect(matchProject('gate', '小米yu7', 'gate', '')).toBe(true);
    expect(matchProject('gate', '小米yu7', 'done', '')).toBe(false);
  });

  it('搜索词要命中', () => {
    expect(matchProject('gate', '小米yu7', 'all', 'yu7')).toBe(true);
    expect(matchProject('gate', '小米yu7', 'all', '比亚迪')).toBe(false);
  });

  it('分组和搜索词要同时满足', () => {
    expect(matchProject('gate', '小米yu7', 'done', 'yu7')).toBe(false);
    expect(matchProject('gate', '小米yu7', 'gate', 'yu7')).toBe(true);
  });
});
