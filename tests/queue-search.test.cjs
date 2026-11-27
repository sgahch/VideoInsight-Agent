const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function dashboard() {
  const nodes = new Map();
  const document = { activeElement: null, body: { dataset: {} }, addEventListener() {} };
  function element(tag = 'div') {
    const node = {
      tagName: tag, children: [], listeners: {}, dataset: {}, value: '', hidden: false,
      className: '', textContent: '', classList: { add() {}, remove() {}, toggle() {} },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      setAttribute(name, value) { this[name] = value; },
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      querySelector() { return null; }, querySelectorAll() { return []; },
      closest() { return null; }, scrollIntoView() {},
      focus() { document.activeElement = this; },
      async fire(type) { return this.listeners[type]?.({ target: this, preventDefault() {} }); },
    };
    return node;
  }
  const node = (id) => {
    if (!nodes.has(id)) nodes.set(id, element());
    return nodes.get(id);
  };
  document.getElementById = node;
  document.createElement = element;
  const fixtures = [
    { task_id: 'failed-video', status: 'failed', title: 'Battery review', request: { url: 'https://www.bilibili.com/video/example', mode: 'transcript' }, created_at: '2026-11-27T04:00:00', error: 'Groq timeout' },
    { task_id: 'completed-video', status: 'succeeded', title: 'Interview notes', request: { url: 'https://www.douyin.com/video/123', mode: 'summary' }, created_at: '2026-11-26T04:00:00' },
  ];
  const overview = () => ({ tasks: fixtures.slice(), counts: { running: 0, queued: 0, succeeded: 1, failed: 1 },
    pool: { core_workers: 2, max_workers: 2, active_workers: 0, queued: 0, queue_capacity: 32 }, engine: { asr_global_workers: 2 } });
  const context = vm.createContext({ document, URL, console, location: { pathname: '/' },
    history: { pushState() {} }, CSS: { escape: (text) => text },
    addEventListener() {}, setTimeout() {}, clearTimeout() {} });
  context.window = context;
  vm.runInContext(fs.readFileSync('web/queue.js', 'utf8'), context);
  const retries = [];
  let retry = async () => {};
  const queue = context.VideoQueue.create({ fetchJson: async () => overview(), title: (task) => task.title,
    usedAt: () => null, onTasks() {}, onRefreshError(error) { throw error; }, stopWatching() {},
    retry: (task) => { retries.push(task.task_id); return retry(task); } });
  const rows = () => node('queueTaskList').children.filter((item) => item.className === 'queue-task-row');
  return { node, document, fixtures, queue, rows, retries, retryWith(fn) { retry = fn; } };
}

test('retry preserves history when a password manager fills the unfocused search field', async () => {
  const page = dashboard();
  await page.queue.refresh();
  assert.equal(page.rows().length, 2);
  page.retryWith(async () => {
    const search = page.node('queueSearch');
    search.value = 'account@example.test';
    await search.fire('input');
    assert.equal(page.rows().length, 2, 'autofilled account text must not hide task history');
    await page.queue.onSubmitted('retried-task');
  });
  const button = page.rows()[0].children[1].children[0];
  button.focus();
  await button.fire('click');
  assert.equal(page.retries.length, 1);
  assert.equal(page.rows().length, 2);
  assert.equal(page.node('queueSearch').value, '');
});

test('intentional search survives polling and ignores later background autofill', async () => {
  const page = dashboard();
  await page.queue.refresh();
  const search = page.node('queueSearch');
  search.focus();
  search.value = 'battery';
  await search.fire('input');
  assert.equal(page.rows().length, 1);
  assert.equal(page.rows()[0].dataset.taskId, 'failed-video');
  page.document.activeElement = null;
  search.value = 'account@example.test';
  await search.fire('input');
  await page.queue.refresh(true);
  assert.equal(page.rows().length, 1);
  assert.equal(search.value, 'battery');
  search.focus();
  search.value = '';
  await search.fire('input');
  assert.equal(page.rows().length, 2);
});

test('silent autofill is reconciled on refresh without changing the active query', async () => {
  const page = dashboard();
  await page.queue.refresh();
  page.node('queueSearch').value = 'account@example.test';
  await page.queue.refresh(true);
  assert.equal(page.rows().length, 2);
  assert.equal(page.node('queueSearch').value, '');
});
