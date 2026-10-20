/* 事件日志:关键词过滤。 */
import { $, $$, debounce } from '../ui/dom.js';
import { initChrome } from '../ui/chrome.js';

initChrome();

const search = $('#log-search');
if (search) {
  search.addEventListener('input', debounce(() => {
    const q = search.value.trim().toLowerCase();
    let n = 0;
    for (const li of $$('#log-list .tl-item')) {
      const show = !q || li.dataset.text.includes(q);
      li.classList.toggle('hidden', !show);
      if (show) n++;
    }
    $('#log-empty').classList.toggle('hidden', n > 0);
  }, 120));
}
