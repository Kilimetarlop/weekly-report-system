/* SPDX-License-Identifier: PolyForm-Strict-1.0.0
   Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed. */
/* 页面小助手（没有脚本时页面照常可用）：
   1. 甘特图打开时自动滚到“今天”附近（今天位于可视区域左侧约三分之一处），任务名列固定不动；
   2. 周报“全部展开 / 全部收起”：要点默认收起，点要点看详细阐述；
   3. 成员管理里在老师和学生之间改身份前弹出确认；
   4. 批量导入后“复制全部”新账号和初始密码；
   5. 页面一直开着时，过了零点（北京时间）红线自动移到新的一天。 */
(function () {
  'use strict';
  document.querySelectorAll('.gantt-scroll').forEach(function (box) {
    var today = box.querySelector('.gantt-today');
    var label = box.querySelector('.gantt-label');
    if (!today || box.scrollWidth <= box.clientWidth) return;
    var labelWidth = label ? label.offsetWidth : 0;
    box.scrollLeft = Math.max(0, today.offsetLeft - labelWidth - (box.clientWidth - labelWidth) / 3);
  });
  // 成员管理：在老师和学生之间切换身份会改变应交名单，保存前确认（硕士、博士、学生之间改只是换标签，不提示）
  document.querySelectorAll('form[data-identity-was]').forEach(function (form) {
    form.addEventListener('submit', function (event) {
      var was = form.getAttribute('data-identity-was'), now = form.elements.identity.value, name = form.getAttribute('data-name');
      var text = '';
      if (was !== 'teacher' && now === 'teacher') text = '把 ' + name + ' 设为老师？以后周报改为选填、不计入提交率；以往没交的周不再算未交，已交的周报保留。';
      if (was === 'teacher' && now !== 'teacher') text = '把 ' + name + ' 改为学生？从本周起需要写周报（本周已过截止就从下周起），以往各周不补。';
      if (text && !window.confirm(text)) event.preventDefault();
    });
  });
  // 复制按钮：把指定文本框的内容复制到剪贴板（批量导入后的新账号和初始密码）
  document.querySelectorAll('[data-copy]').forEach(function (button) {
    var source = document.querySelector(button.getAttribute('data-copy'));
    if (!source) return;
    button.hidden = false;
    button.addEventListener('click', function () {
      var done = function () { button.textContent = '已复制，可以粘贴到微信或表格里'; };
      var fallback = function () { source.select(); document.execCommand('copy'); done(); };
      if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(source.value).then(done, fallback);
      else fallback();
    });
  });
  var beijingDay = function () { return Math.floor((Date.now() + 8 * 3600 * 1000) / 86400000); };
  var loadedDay = beijingDay();
  var moveToday = function () {
    var days = beijingDay() - loadedDay;
    if (!days) return;
    document.querySelectorAll('.gantt-today[data-date]').forEach(function (line) {
      var offset = Number(line.getAttribute('data-offset')) + days / 7;
      if (offset >= Number(line.getAttribute('data-weeks'))) return;
      var parts = line.getAttribute('data-date').split('-').map(Number);
      var day = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + days));
      line.style.setProperty('--offset', offset);
      line.title = '今天 ' + day.toISOString().slice(0, 10);
      var flag = line.querySelector('.gantt-today-flag');
      if (flag) flag.textContent = (day.getUTCMonth() + 1) + '/' + day.getUTCDate();
    });
  };
  setInterval(moveToday, 60000);
  document.addEventListener('visibilitychange', moveToday);
  document.querySelectorAll('[data-expand-all]').forEach(function (button) {
    var scope = document.querySelector(button.getAttribute('data-expand-all'));
    if (!scope) return;
    button.hidden = false;
    button.addEventListener('click', function () {
      var items = scope.querySelectorAll('details.point-view');
      var open = !Array.prototype.every.call(items, function (item) { return item.open; });
      items.forEach(function (item) { item.open = open; });
      button.textContent = open ? '全部收起' : '全部展开';
    });
  });
})();
