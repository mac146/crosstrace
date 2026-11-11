// Vercel AI SDK: streamText with the OpenAI chat model, awaiting the full text (errors go to onError by default)
import { streamText } from 'ai';
import { openai } from '@ai-sdk/openai';
const errors = [];
try {
  const r = streamText({ model: openai.chat('m'), prompt: 'tell me', onError: ({ error }) => errors.push(String(error?.constructor?.name)) });
  const out = await r.text;
  console.log(JSON.stringify(errors.length ? { ok: false, error: 'onError:' + errors.join(','), msg: 'text resolved: ' + out.length + ' chars' } : { ok: true, out }));
} catch (e) { console.log(JSON.stringify({ ok: false, error: e.constructor.name, msg: String(e.message).slice(0, 200) })); }
