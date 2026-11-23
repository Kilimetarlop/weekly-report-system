/* SPDX-License-Identifier: PolyForm-Strict-1.0.0
   Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed. */
/* 周报填写：每条 = 要点（一行，不超过 50 字）+ 详细阐述（计有效字符）。
   每个页面固定 period；不在 localStorage 留存科研正文。停笔约 2 秒自动暂存草稿；提交前先排空已排队的保存。 */
(() => {
  'use strict';
  const form = document.querySelector('#editor');
  const status = document.querySelector('#status');
  const error = document.querySelector('#error');
  const payload = JSON.parse(document.querySelector('#payload').textContent);
  const period = form.dataset.period;
  const pointMax = Number(form.dataset.pointMax || 50);
  const count = window.effectiveChars;
  const fields = [...form.querySelectorAll('fieldset')];
  let revision = Number(form.dataset.revision);
  let dirty = false, timer = null, pending = Promise.resolve(), submitting = false, token = null, changeSerial = 0;

  const asItem = (value) => (typeof value === 'string' ? { point: '', detail: value } : { point: value.point || '', detail: value.detail || '' });

  function renumber(field) {
    [...field.querySelectorAll('.point-no')].forEach((element, index) => { element.textContent = (index + 1) + '.'; });
  }

  function add(field, item = { point: '', detail: '' }) {
    const legend = field.querySelector('legend').textContent;
    const box = document.createElement('div');
    box.className = 'point';
    const head = document.createElement('div');
    head.className = 'point-head';
    const number = document.createElement('span');
    number.className = 'point-no';
    const point = document.createElement('input');
    point.type = 'text';
    point.className = 'point-input';
    point.maxLength = pointMax;
    // 每一项有自己的灰色示例（来自 domain.GUIDES），开始输入就消失
    point.placeholder = '要点（一句话）' + (field.dataset.pointPlaceholder ? '，' + field.dataset.pointPlaceholder : '');
    point.title = '要点不超过 ' + pointMax + ' 字';
    point.value = item.point;
    point.setAttribute('aria-label', legend + '要点');
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary small';
    remove.textContent = '删除';
    const detail = document.createElement('textarea');
    detail.rows = 3;
    detail.placeholder = '详细阐述' + (field.dataset.detailPlaceholder ? '，' + field.dataset.detailPlaceholder : '：具体做了什么、结果如何、依据或数据……');
    detail.value = item.detail;
    detail.setAttribute('aria-label', legend + '详细阐述');
    const counter = document.createElement('span');
    counter.className = 'count';
    const refresh = () => { counter.textContent = '详细阐述 ' + count(detail.value) + ' 个有效字符'; };
    const touched = () => { field.querySelector('.none').checked = false; changed(); };
    refresh();
    point.addEventListener('input', touched);
    detail.addEventListener('input', () => { refresh(); touched(); });
    remove.addEventListener('click', () => {
      if ((point.value.trim() || detail.value.trim()) && !confirm('确认删除这一条？')) return;
      box.remove();
      renumber(field);
      changed();
    });
    head.append(number, point, remove);
    box.append(head, detail, counter);
    field.querySelector('.items').append(box);
    renumber(field);
    return box;
  }

  fields.forEach((field) => {
    const item = payload[field.dataset.section][field.dataset.key];
    field.querySelector('.none').checked = item.state === 'NONE';
    item.items.map(asItem).forEach((entry) => add(field, entry));
    if (!item.items.length && item.state !== 'NONE') add(field);
    field.querySelector('.add').addEventListener('click', () => {
      field.querySelector('.none').checked = false;
      add(field).querySelector('.point-input').focus();
      changed();
    });
    field.querySelector('.none').addEventListener('change', (event) => {
      if (event.target.checked && [...field.querySelectorAll('.point-input, textarea')].some((input) => input.value.trim())) {
        if (!confirm('勾选暂无将清除这一小项的全部内容，是否继续？')) { event.target.checked = false; return; }
      }
      if (event.target.checked) field.querySelector('.items').replaceChildren();
      else if (!field.querySelector('.point')) add(field);
      changed();
    });
  });

  function body() {
    const result = { schema_version: 2, research: {}, project: {} };
    fields.forEach((field) => {
      const none = field.querySelector('.none').checked;
      const items = [...field.querySelectorAll('.point')].map((box) => ({ point: box.querySelector('.point-input').value, detail: box.querySelector('textarea').value }));
      const filled = items.some((entry) => entry.point.trim() || entry.detail.trim());
      result[field.dataset.section][field.dataset.key] = { state: none ? 'NONE' : filled ? 'ITEMS' : 'UNSET', items: none ? [] : items };
    });
    return result;
  }

  function changed() {
    dirty = true;
    token = null;
    changeSerial++;
    status.textContent = '有未保存修改';
    clearTimeout(timer);
    if (!submitting) timer = setTimeout(() => save(false), 2000);
  }

  async function send(submit) {
    const serial = changeSerial;
    const content = body();
    status.textContent = submit ? '正在提交…' : '保存中…';
    error.textContent = '';
    const key = submit ? (token || (token = crypto.randomUUID())) : undefined;
    try {
      const response = await fetch(`/my/weeks/${period}/${submit ? 'submit' : 'draft'}/`, {
        method: submit ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value },
        body: JSON.stringify({ payload: content, expected_revision: revision, idempotency_key: key }),
      });
      const data = await response.json();
      if (!response.ok) throw Error(data.error || '请求失败，请保留当前文字');
      revision = data.revision;
      if (changeSerial === serial) dirty = false;
      status.textContent = submit ? '已正式提交' : '已保存 ' + new Date(data.saved_at).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai' });
      if (submit) { dirty = false; location.reload(); }
    } catch (failure) {
      status.textContent = '保存或提交失败；当前文字仍在页面';
      error.textContent = failure.message;
      throw failure;
    }
  }

  function save(submit) {
    clearTimeout(timer);
    if (submitting) return pending;
    if (!submit && !dirty) return pending;
    const controls = () => form.querySelectorAll('button, textarea, input');
    if (submit) { submitting = true; controls().forEach((element) => { element.disabled = true; }); }
    pending = pending.catch(() => {}).then(() => send(submit)).catch(() => {}).finally(() => {
      if (submit) { submitting = false; controls().forEach((element) => { element.disabled = false; }); }
    });
    return pending;
  }

  document.querySelector('#save').addEventListener('click', () => save(false));
  document.querySelector('#submit').addEventListener('click', () => save(true));
  window.addEventListener('beforeunload', (event) => { if (dirty) { event.preventDefault(); event.returnValue = '有尚未保存的修改'; } });
})();
