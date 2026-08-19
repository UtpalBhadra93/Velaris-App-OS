// Adversarial cases against the GENERATED url.ts. The base URL is the one
// place a valid bearer token could be aimed at a host that should never see
// it, so the interesting cases are the ones that should be REFUSED.
const { checkBaseUrl, originOf, pathIsSafe, isUuid } = require(process.env.GENERATED_OUT + '/url.js');

let failures = 0;
const ok = (n, c, x) => { if (c) console.log('  PASS  ' + n); else { failures++; console.log('  FAIL  ' + n + (x !== undefined ? '  ' + JSON.stringify(x) : '')); } };

console.log('accepted');
for (const u of [
  'https://velaris.example.com',
  'https://velaris.example.com/',
  'https://velaris.example.com:8443',
  'https://velaris.example.com/velaris',
  'http://localhost:8200',
  'http://127.0.0.1:8200',
  'http://192.168.1.40:8200',
  'http://10.0.0.5:8200',
  'http://172.20.0.9:8200',
  'http://dev-box.local:8200',
]) {
  const r = checkBaseUrl(u);
  ok('accepts ' + u, r.ok === true, r);
}

console.log('refused');
for (const [u, why] of [
  ['http://attacker.example.com', 'plain http to a public host'],
  ['http://8.8.8.8', 'plain http to a public IP'],
  ['ftp://velaris.example.com', 'not http(s)'],
  ['javascript:alert(1)', 'a script URL'],
  ['https://user:pass@velaris.example.com', 'credentials in the URL'],
  ['https://velaris.example.com?next=https://evil.test', 'a query string'],
  ['https://velaris.example.com#x', 'a fragment'],
  ['https://velaris.example.com\\@evil.test', 'a backslash the browser reads as a separator'],
  ['https://velaris.example.com\t/evil', 'an embedded tab'],
  ['https://velaris.example\n.com', 'an embedded newline'],
  ['https://velaris.example.com\n@evil.test', 'a newline the anchor must not let past'],
  ['https://velaris.example.com /evil', 'an embedded space'],
  ['//evil.test', 'a scheme-relative URL'],
  ['velaris.example.com', 'no scheme at all'],
  ['https://velaris.example.com:99999', 'an impossible port'],
  ['', 'nothing'],
  ['   ', 'whitespace'],
]) {
  const r = checkBaseUrl(u);
  ok('refuses ' + why, r.ok === false, { u, r });
}

console.log('case is not significant in a host name');
ok('accepts HTTPS://Velaris.Example.com', checkBaseUrl('HTTPS://Velaris.Example.com').ok === true, checkBaseUrl('HTTPS://Velaris.Example.com'));
ok('origin comes back lower-cased', originOf('HTTPS://Velaris.Example.com') === 'https://velaris.example.com', originOf('HTTPS://Velaris.Example.com'));

console.log('canonical form');
ok('trailing slash dropped', checkBaseUrl('https://a.example.com/').baseUrl === 'https://a.example.com');
ok('origin excludes the path', originOf('https://a.example.com/velaris') === 'https://a.example.com');
ok('a refused URL has no origin', originOf('http://evil.test') === '');
ok('different port is a different origin',
   originOf('http://localhost:8200') !== originOf('http://localhost:8299'));

console.log('request paths');
ok('a normal path', pathIsSafe('/cases'));
ok('a query the app builds', pathIsSafe('/cases?page=1&page_size=50'));
ok('a path with an id', pathIsSafe('/cases/70c463e9-1275-41fb-aaf4-32dc19f6d7c8/resolve'));
ok('refuses a protocol-relative jump', !pathIsSafe('//evil.test/x'));
ok('refuses traversal', !pathIsSafe('/cases/../../admin'));
ok('refuses a path that is not rooted', !pathIsSafe('cases'));
ok('refuses a full URL as a path', !pathIsSafe('https://evil.test/x'));

console.log('case ids');
ok('accepts a uuid', isUuid('70c463e9-1275-41fb-aaf4-32dc19f6d7c8'));
ok('refuses a path fragment', !isUuid('../../admin'));
ok('refuses undefined', !isUuid(undefined));

console.log(failures === 0 ? '\nALL PASS' : '\n' + failures + ' FAILED');
process.exit(failures ? 1 : 0);
