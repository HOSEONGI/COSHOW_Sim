import test from 'node:test';
import assert from 'node:assert/strict';
import {buttons,commandReply,landingText} from '../static/js/admin-model.js';
test('every run state follows command table and LANDING defeats retry timeout',()=>{
  const matrix={IDLE:['preflight','reset'],CHECKING:['estop','reset'],READY:['start','estop','reset'],RUNNING:['estop','reset'],LANDING:[],DONE:['estop','reset'],ABORTED:['reset']};
  for(const [state,expected] of Object.entries(matrix))assert.deepEqual(Object.keys(buttons({run:{state},checklist:[]},true,null)).filter(k=>buttons({run:{state},checklist:[]},true,null)[k]),expected);
  assert.ok(Object.values(buttons({run:{state:'READY'},checklist:[]},false,null)).every(v=>!v));
  assert.equal(buttons({run:{state:'READY'},checklist:[{blocking:true,ok:false}]},true,null).start,false);
  assert.deepEqual(buttons({run:{state:'RUNNING'}},true,{elapsed:1001}),{preflight:false,start:false,estop:true,reset:false});
  assert.ok(Object.values(buttons({run:{state:'LANDING'}},true,{elapsed:1001})).every(v=>!v));
});
test('snapshot ack ignores history and matches only the pending command',()=>{
  const events=[{t:1,text:'cmd:start accepted'},{t:2,text:'cmd:reset rejected(착륙 중)'}];
  assert.equal(commandReply(events,{cmd:'start',seen:new Set(['1|cmd:start accepted'])}),null);
  assert.equal(commandReply(events,{cmd:'reset',seen:new Set()}),events[1]);
  assert.match(landingText({run:{elapsed_s:2.5},events:[{text:'착륙 시퀀스 시작'},{text:'land 전송 2/4'}]}),/2\.5초.*2\/4/);
  assert.match(landingText({run:{elapsed_s:.1},events:[{text:'land 전송 4/4'},{text:'착륙 시퀀스 시작'}]}),/land 전송 대기/);
});
