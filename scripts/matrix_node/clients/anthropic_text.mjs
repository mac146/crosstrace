// @anthropic-ai/sdk: the messages.stream() helper and its final text
import Anthropic from '@anthropic-ai/sdk';
try {
  const s = new Anthropic().messages.stream({ model: 'm', max_tokens: 100, messages: [{ role: 'user', content: 'tell me' }] });
  console.log(JSON.stringify({ ok: true, out: await s.finalText() }));
} catch (e) { console.log(JSON.stringify({ ok: false, error: e.constructor.name, msg: String(e.message).slice(0, 200) })); }
