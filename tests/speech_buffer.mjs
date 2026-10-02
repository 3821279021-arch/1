import {readFile} from 'node:fs/promises';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = await readFile(new URL('../app/static/js/speech-buffer.js',import.meta.url),'utf8');
const {SpeechBuffer} = await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));

test('standard display is paced at 16 characters per second',()=>{
 const b=new SpeechBuffer();b.put('a',1,'甲'.repeat(100),true);
 assert.equal(b.tick(0).delta.length,1);assert.equal(b.tick(1000).item.shown,17);
});
test('punctuation pauses after comma and full stop',()=>{
 const b=new SpeechBuffer();b.put('a',1,'甲，乙。丙',true);
 b.tick(0);assert.equal(b.tick(63).delta,'，');assert.equal(b.tick(200).delta,'');
 assert.equal(b.tick(245).delta,'乙');assert.equal(b.tick(308).delta,'。');assert.equal(b.tick(600).delta,'');
 assert.equal(b.tick(670).delta,'丙');
});
test('slow upstream delivery shows characters as they arrive',()=>{
 const b=new SpeechBuffer();b.put('a',1,'甲');assert.equal(b.tick(0).delta,'甲');b.tick(400);
 b.append('a',1,'乙');assert.equal(b.tick(500).delta,'乙');
});
test('finished text is held for a second before the next seat',()=>{
 const b=new SpeechBuffer();b.put('a',1,'甲',true);b.put('b',2,'乙',true);
 assert.equal(b.tick(0).item.pid,1);assert.equal(b.tick(999).item.pid,1);assert.equal(b.tick(1000).item.pid,2);
});
test('TTS completion keeps the displayed speaker until audio finishes',()=>{
 const b=new SpeechBuffer();b.put('a',1,'甲',true);b.put('b',2,'乙',true);b.tick(0);
 assert.equal(b.tick(2000,true).item.pid,1);assert.equal(b.tick(2001,false).item.pid,2);
});
test('instant mode catches up without punctuation or end holds',()=>{
 const b=new SpeechBuffer('instant');b.put('a',1,'甲，乙。丙',true);b.put('b',2,'下一位',true);
 assert.equal(b.tick(0).delta,'甲，乙。丙');assert.equal(b.tick(1,true).item.pid,2);
});
test('snapshot replacement and duplicate finishes do not repeat speech',()=>{
 const b=new SpeechBuffer('instant');b.put('a',1,'甲');b.tick(0);b.put('a',1,'甲乙',true);
 assert.equal(b.tick(1).delta,'乙');b.tick(2);b.put('a',1,'甲乙',true);assert.equal(b.tick(3).item,null);
});
test('surrogate pairs are displayed as complete characters',()=>{
 const b=new SpeechBuffer();b.put('a',1,'🐺甲',true);assert.equal(b.tick(0).delta,'🐺');assert.equal(b.tick(63).delta,'甲');
});
test('reset cancels queued presentation and permits retry of the same turn',()=>{
 const b=new SpeechBuffer();b.put('a',1,'旧发言');b.put('b',2,'等待');b.tick(0);b.reset();
 assert.equal(b.tick(10).item,null);b.put('a',1,'重试');assert.equal(b.tick(11).delta,'重');
});
test('long backlog stays bounded and reports skipped early text',()=>{
 const b=new SpeechBuffer();for(let i=0;i<90;i++)b.put(String(i),i,'片段',true);
 assert.equal(b.queue.length,80);assert.equal(b.skipped,10);assert.equal(b.tick(0).item.pid,10);
});
