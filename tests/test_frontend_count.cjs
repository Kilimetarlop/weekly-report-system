/* SPDX-License-Identifier: PolyForm-Strict-1.0.0
   Copyright (c) 2026 Kilimetarlop. All rights reserved except as licensed. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const count=require('../static/count.js');
const fixtures=JSON.parse(fs.readFileSync(__dirname+'/unicode-fixtures.json','utf8'));
fixtures.forEach(([text,expected])=>assert.equal(count(text),expected));
console.log(`${fixtures.length} shared frontend Unicode fixtures passed`);
