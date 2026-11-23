/* SPDX-License-Identifier: PolyForm-Strict-1.0.0
   Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed. */
/* 渐进增强：不依赖脚本也可登录，不在浏览器存储账号或密码。 */
(()=>{'use strict';const field=document.getElementById('id_password'),toggle=document.querySelector('.password-toggle');if(!field||!toggle)return;toggle.hidden=false;toggle.addEventListener('click',()=>{const reveal=field.type==='password';field.type=reveal?'text':'password';toggle.textContent=reveal?'隐藏':'显示';toggle.setAttribute('aria-pressed',String(reveal));toggle.setAttribute('aria-label',reveal?'隐藏密码':'显示密码');});window.addEventListener('pagehide',()=>{field.type='password';});})();
