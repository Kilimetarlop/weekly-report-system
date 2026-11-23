/* SPDX-License-Identifier: PolyForm-Strict-1.0.0
   Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed. */
(function(root){'use strict';function effectiveChars(text){return [...text].filter(c=>/[\p{L}\p{Nd}]/u.test(c)).length;}if(typeof module!=='undefined'&&module.exports)module.exports=effectiveChars;else root.effectiveChars=effectiveChars;})(globalThis);
