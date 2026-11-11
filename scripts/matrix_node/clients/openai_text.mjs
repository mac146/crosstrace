// openai-node: stream a text answer and join the deltas
import OpenAI from 'openai';
try {
  const s = await new OpenAI().chat.completions.create({ model: 'm', messages: [{ role: 'user', content: 'tell me' }], stream: true });
  let out = '';
  for await (const e of s) out += e.choices[0]?.delta?.content ?? '';
  console.log(JSON.stringify({ ok: true, out }));
} catch (e) { console.log(JSON.stringify({ ok: false, error: e.constructor.name, msg: String(e.message).slice(0, 200) })); }
