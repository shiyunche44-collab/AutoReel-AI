import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../app/web/static/js/api.js';

function reply(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('api 请求', () => {
  it('成功时返回后端的 JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply({ id: 'p1', state: 'GATE_A' })),
    );
    await expect(api.get('/api/projects')).resolves.toEqual({ id: 'p1', state: 'GATE_A' });
  });

  it('成功但响应体是空的时候返回空对象', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply('')),
    );
    await expect(api.get('/api/health')).resolves.toEqual({});
  });

  it('成功但响应体不是 JSON 时,把原文塞进 detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply('不是 JSON')),
    );
    await expect(api.get('/api/x')).resolves.toEqual({ detail: '不是 JSON' });
  });

  it('失败时把后端的 detail 抛出来', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply({ detail: '项目不存在' }, { ok: false, status: 404 })),
    );
    await expect(api.get('/api/projects/x')).rejects.toThrow('项目不存在');
  });

  it('422 的 detail 是数组时,逐条拼成一句话', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        reply({ detail: [{ msg: '题材不能为空' }, { msg: '时长不合法' }] }, { ok: false, status: 422 }),
      ),
    );
    await expect(api.post('/api/projects', {})).rejects.toThrow('题材不能为空;时长不合法');
  });

  it('422 的数组元素没有 msg 时退回 JSON 串', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply({ detail: [{ loc: ['body', 'subject'] }] }, { ok: false, status: 422 })),
    );
    await expect(api.post('/api/projects', {})).rejects.toThrow('{"loc":["body","subject"]}');
  });

  it('失败且没有 detail 时给出状态码', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => reply({}, { ok: false, status: 500 })),
    );
    await expect(api.get('/api/x')).rejects.toThrow('请求失败(HTTP 500)');
  });

  it('GET 不带 body,也不带 content-type', async () => {
    const fetchMock = vi.fn(async () => reply({}));
    vi.stubGlobal('fetch', fetchMock);
    await api.get('/api/x');
    const [, init] = fetchMock.mock.calls[0];
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(init.headers).toBeUndefined();
  });

  it('POST 带 body 时发 JSON 并带上 content-type', async () => {
    const fetchMock = vi.fn(async () => reply({}));
    vi.stubGlobal('fetch', fetchMock);
    await api.post('/api/projects', { subject: '小米YU7' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/projects');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({ subject: '小米YU7' });
  });

  it('PATCH / DELETE 走对方法', async () => {
    const fetchMock = vi.fn(async () => reply({}));
    vi.stubGlobal('fetch', fetchMock);
    await api.patch('/api/projects/p1/script', { segments: [] });
    await api.del('/api/projects/p1');
    expect(fetchMock.mock.calls[0][1].method).toBe('PATCH');
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE');
  });
});
