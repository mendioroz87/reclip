const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const html = fs.readFileSync('templates/index.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

async function scenario({ status, contentType, filename, expected }) {
  const requests = [];
  const clicks = [];
  const states = [];
  const context = {
    document: {
      getElementById: () => ({ addEventListener() {} }),
      createElement: () => ({ click() { clicks.push(this.download); }, remove() {} }),
      body: { appendChild() {} },
    },
    fetch: async (url, options) => {
      requests.push({ url, method: options.method });
      return { ok: status >= 200 && status < 300, status, headers: { get: () => contentType } };
    },
    states,
  };
  vm.createContext(context);
  vm.runInContext(script + '\nrenderCard = idx => states.push(cardData[idx].status);', context);
  context.filename = filename;
  vm.runInContext("cardData = [{jobId: 'sample', filename, status: 'done'}];", context);
  await vm.runInContext('saveCard(0)', context);
  assert.equal(vm.runInContext('cardData[0].status', context), expected);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].method, 'HEAD');
  assert.equal(clicks.length, expected === 'done' ? 1 : 0);
  if (expected === 'error') assert.ok(vm.runInContext('cardData[0].error.length > 0', context));
  console.log(`PASS ${status} ${contentType} ${filename}: ${expected}`);
}

(async () => {
  await scenario({ status: 500, contentType: 'text/html', filename: 'video.mp4', expected: 'error' });
  await scenario({ status: 404, contentType: 'application/json', filename: 'video.mp4', expected: 'error' });
  await scenario({ status: 200, contentType: 'text/html', filename: 'video.mp4', expected: 'error' });
  await scenario({ status: 200, contentType: 'audio/mpeg', filename: 'video.mp4', expected: 'error' });
  await scenario({ status: 200, contentType: 'video/mp4', filename: 'Salesforce Microfrontends.mp4', expected: 'done' });
  await scenario({ status: 200, contentType: 'audio/mpeg', filename: 'Audio.mp3', expected: 'done' });
})().catch(error => { console.error(error); process.exitCode = 1; });

async function checkMultipleLinks() {
  const playlist = 'https://www.youtube.com/playlist?list=example';
  const video = 'https://site.example/watch?list=item';
  const input = { value: ['invalid', playlist, video, video].join('\n') };
  const button = {};
  const requests = [];
  const context = { URL, document: { getElementById: id =>
    id === 'urls' ? Object.assign(input, { addEventListener() {} }) : button },
    fetch: async (endpoint, options) => {
      requests.push([endpoint, JSON.parse(options.body).url]);
      return { json: async () => endpoint.endsWith('playlist') ? { error: 'Access denied' } : { title: 'Clip' } };
    } };
  vm.createContext(context);
  vm.runInContext(script + '\nrenderCard = () => {};', context);
  await vm.runInContext('go()', context);
  assert.deepEqual(requests, [['/api/playlist', playlist], ['/api/info', video]]);
  assert.equal(vm.runInContext('cardData.map(c => c.status).join(",")', context), 'info-error,info-error,ready');
  assert.equal(vm.runInContext("friendlyError('Access denied by the platform or network policy.')", context),
    'Access denied by the platform or network policy.');
  assert.equal(button.disabled, false);
  console.log('PASS multiple links, playlist failure, deduplication and recovery');
}
checkMultipleLinks().catch(error => { console.error(error); process.exitCode = 1; });
