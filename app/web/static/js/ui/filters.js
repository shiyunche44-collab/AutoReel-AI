/* 筛选判断:页面里"哪些条要显示"的规则集中在这里,页面只负责画 DOM。
   抽出来是为了能单测 —— 这些判断错了不会报错,只是默默少显示几条。 */

/** 还在跑的状态(渲染看板"进行中"筛选用) */
export const LIVE_STATUSES = ['submitted', 'queued', 'running'];

/**
 * 一拍要不要显示。filter: all | live | 具体状态。
 * @param {string} status
 * @param {string} filter
 * @returns {boolean}
 */
export function matchShot(status, filter) {
  if (filter === 'all') return true;
  if (filter === 'live') return LIVE_STATUSES.includes(status);
  return status === filter;
}

/**
 * 一条事实要不要显示:范围(scope)对上,而且搜索词命中。
 * @param {string} scope 这条事实的范围
 * @param {string} text 这条事实的可搜索文本(已转小写)
 * @param {string} wantScope 当前选的范围,all = 不限
 * @param {string} wantQuery 当前搜索词,空 = 不限
 * @returns {boolean}
 */
export function matchFact(scope, text, wantScope, wantQuery) {
  if (wantScope !== 'all' && scope !== wantScope) return false;
  if (wantQuery && !text.includes(wantQuery)) return false;
  return true;
}

/**
 * 一个项目卡片要不要显示:搜索词命中,而且分组对上。
 * @param {string} group 卡片的分组
 * @param {string} search 卡片的可搜索文本(已转小写)
 * @param {string} wantGroup 当前选的分组,all = 不限
 * @param {string} wantQuery 当前搜索词,空 = 不限
 * @returns {boolean}
 */
export function matchProject(group, search, wantGroup, wantQuery) {
  if (wantQuery && !search.includes(wantQuery)) return false;
  if (wantGroup !== 'all' && group !== wantGroup) return false;
  return true;
}
