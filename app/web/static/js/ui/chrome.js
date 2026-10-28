/* 所有页面共有的外壳行为:主题切换、状态词表、套餐到期提醒。 */
import { $, $$, readJSON, isMac } from './dom.js';

const THEME_KEY = 'AutoReel-AI.theme';

function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function initTheme() {
  const btn = $('#theme-toggle');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const next = currentTheme() === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* 隐私模式下写不了,忽略 */
    }
  });
}

function initPlanPill() {
  const pill = $('#plan-pill');
  if (!pill) return;
  const exp = new Date(`${pill.dataset.expires || ''}T23:59:59`);
  if (Number.isNaN(exp.getTime())) return;
  const days = Math.ceil((exp.getTime() - Date.now()) / 86400000);
  const label = pill.querySelector('span');
  pill.classList.remove('tone-neutral');
  if (days < 0) {
    pill.classList.add('tone-danger');
    label.textContent = '套餐已过期';
  } else if (days <= 7) {
    pill.classList.add('tone-warning');
    label.textContent = days === 0 ? '套餐今天到期' : `套餐 ${days} 天后到期`;
  } else {
    pill.classList.add('tone-neutral');
  }
}

/** 状态 → { label, tone, step, live, group },与 _macros.html 同源。 */
export const STATE_META = readJSON('state-meta', {});
export function stateMeta(state) {
  return STATE_META[state] || { label: state, tone: 'neutral', step: -1, live: false, group: 'other' };
}

/** 就地更新一个状态徽标。 */
export function paintBadge(el, state) {
  if (!el) return;
  const m = stateMeta(state);
  el.className = `badge tone-${m.tone}${m.live ? ' is-live' : ''}`;
  el.title = state;
  el.dataset.state = state;
  el.lastChild.textContent = m.label;
}

export function initChrome() {
  initTheme();
  initPlanPill();
  if (isMac)
    $$('.kbd').forEach((k) => {
      k.textContent = k.textContent.replace('Ctrl ', '⌘');
    });
}
