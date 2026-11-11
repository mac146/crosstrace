// Stage 21 client: N sequential chat calls with one openai-node client; prints one JSON line per call (ms).
// usage: node client.mjs <openai-new|openai-old> <stream|nonstream> <n> <baseURL> <gap_ms>
const [pkg, mode, n, baseURL, gap] = process.argv.slice(2);
const { default: OpenAI } = await import(pkg);
const client = new OpenAI({ apiKey: "mock", baseURL, maxRetries: 0 });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (let i = 0; i < Number(n); i++) {
  const t0 = performance.now();
  let text = "";
  if (mode === "stream") {
    const s = await client.chat.completions.create({ model: "mock", messages: [{ role: "user", content: "hi" }], stream: true });
    for await (const ev of s) text += ev.choices[0]?.delta?.content ?? "";
  } else {
    const r = await client.chat.completions.create({ model: "mock", messages: [{ role: "user", content: "hi" }] });
    text = r.choices[0].message.content;
  }
  console.log(JSON.stringify({ i, ms: performance.now() - t0, chars: text.length }));
  await sleep(Number(gap));
}
