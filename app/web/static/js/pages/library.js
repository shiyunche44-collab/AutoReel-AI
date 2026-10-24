/* 素材库:池子列表的相对时间 + 池子详情按视角 / 可用 / 关键词筛选。 */
import { $, $$, relTime, debounce } from '../ui/dom.js';
import { initChrome } from '../ui/chrome.js';
import { toast } from '../ui/toast.js';
import { api } from '../api.js';

initChrome();

/* 懂车帝图库、抖音官方号投稿要登录:弹出浏览器,登录成功后窗口自动关 */
$$('[data-login]').forEach((btn) =>
  btn.addEventListener('click', async () => {
    try {
      const r = await api.post(`/api/sources/login/${btn.dataset.login}`);
      toast(r.message, { type: 'brand', duration: 8000 });
    } catch (err) {
      toast(err.message, { type: 'danger' });
    }
  }),
);

$$('.rel-time').forEach((el) => {
  el.textContent = relTime(el.dataset.ts);
});

const grid = $('#asset-grid');
if (grid) {
  const assets = $$('.asset', grid);
  let view = 'all';
  let query = '';

  const apply = () => {
    let shown = 0;
    for (const a of assets) {
      const hitView =
        view === 'all' ||
        (view === 'usable' ? a.dataset.usable === '1' : a.dataset.view === view && a.dataset.usable === '1');
      const show = hitView && (!query || a.dataset.text.includes(query));
      a.classList.toggle('hidden', !show);
      if (show) shown++;
    }
    $('#asset-empty').classList.toggle('hidden', shown > 0);
  };

  $$('#view-filters .chip').forEach((chip) =>
    chip.addEventListener('click', () => {
      view = chip.dataset.view;
      $$('#view-filters .chip').forEach((c) => c.setAttribute('aria-pressed', String(c === chip)));
      apply();
    }),
  );

  const search = $('#asset-search');
  search.addEventListener(
    'input',
    debounce(() => {
      query = search.value.trim().toLowerCase();
      apply();
    }, 120),
  );
}
