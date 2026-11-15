import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import test from 'node:test';

test('direct-download entry and changed modules use the same fresh release cache key', () => {
  const release = '20261115-sharing-preview';
  const index = readFileSync(new URL('../dist/index.html', import.meta.url), 'utf8');
  const app = readFileSync(new URL('../dist/app.js', import.meta.url), 'utf8');
  const entry = [...index.matchAll(/<script\b[^>]*\bsrc=["']([^"']+)["']/g)]
    .map(match => new URL(match[1], 'https://framekeep.test/'))
    .find(url => url.pathname === '/app.js');
  assert.ok(entry, 'HTML must load the application entry');
  assert.equal(entry.searchParams.get('v'), release);
  const imports = [...app.matchAll(/\bfrom\s+["']([^"']+)["']/g)]
    .map(match => new URL(match[1], 'https://framekeep.test/app.js'));
  for (const module of ['/model.js', '/intake-results.js']) {
    const source = imports.find(url => url.pathname === module);
    assert.ok(source, `entry must import ${module}`);
    assert.equal(source.searchParams.get('v'), entry.searchParams.get('v'), `${module} must match the HTML release`);
  }
});
