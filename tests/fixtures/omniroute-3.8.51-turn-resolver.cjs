/*
MIT License

Copyright (c) 2026 diegosouzapw

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

*/
// Resolver factory from the pinned OmniRoute 3.8.51 image, webpack module 121086.
module.exports=(a,b,c)=>{"use strict";c.d(b,{z:()=>j});var d=c(33653),e=c(766025),f=c(348438);function g(a){return a&&"object"==typeof a?a:null}function h(a){let b=g(a);return b?(0,f.OI)(b):[]}function i(a,b){for(let c of b){let b=(0,f.vm)(c);a.has(b)||a.set(b,{textPreview:c.text,blockKind:c.blockKind,toolName:c.toolName})}}function j(a){let b=new Map,c=[...new Set(a.map(a=>a.lastCorrelationId).filter(a=>!!a))];if(0===c.length)return b;let j=(0,d.getDbInstance)(),k=c.map(()=>"?").join(","),l=j.prepare(`SELECT correlation_id, artifact_relpath FROM call_logs
       WHERE correlation_id IN (${k}) AND artifact_relpath IS NOT NULL
       ORDER BY timestamp ASC`).all(...c),m=new Map;for(let a of l)m.has(a.correlation_id)||m.set(a.correlation_id,a.artifact_relpath);for(let a of m.values()){let{artifact:c,state:d}=(0,e.readCallArtifact)(a);if("ready"!==d)continue;let j=g(c?.pipeline);i(b,h(g(j?.clientRawRequest)?.body)),i(b,function(a){let b=g(a);if(!b)return[];let c=g(b.summary),d=Array.isArray(b.output)?b.output:c?.output;return Array.isArray(d)?(0,f.OI)({input:d}):[]}(j?.clientResponse)),i(b,function(a){let b=g(a);return[...h(b),...function(a){if(!Array.isArray(a))return[];let b=[];for(let c of a){let a=g(c)??{};if("assistant"===a.role&&Array.isArray(a.tool_calls))for(let c of a.tool_calls){let a=g(g(c)?.function),d=a?.arguments;"string"==typeof d&&d&&b.push({role:"tool",text:d,blockKind:"tool_use",toolName:"string"==typeof a?.name?a.name:null})}}return b}(b?.messages)]}(g(j?.providerRequest)?.body))}return b}};
