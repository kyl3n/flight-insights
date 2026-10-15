import { expect, test } from '@playwright/test';
import { execFile } from 'node:child_process';
import { resolve } from 'node:path';
import { promisify } from 'node:util';
import { connectEngine } from './engine-harness';

test('research searches pinned local passages and requires explicit consent for an online check', async ({page}) => {
  const engine = await connectEngine(page, {boundaryOverride: method => method === 'research.refresh'
    ? {__q22TransportError: {code: 'TEST_OFFLINE', message: 'Official source check unavailable in this offline test.'}}
    : undefined});
  try {
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button', {name: 'Research library', exact: true}).click();
    const library = page.locator('.research-workspace');
    await expect(library.getByRole('heading', {name: 'Vibration, clipping and mechanical causes', exact: true})).toBeVisible();
    await expect(library.locator('.research-source')).toHaveCount(3);
    await expect(library.getByText('Bundled reviewed excerpt', {exact: true})).toHaveCount(3);
    for (const link of await library.getByRole('link', {name: 'Open this exact official source revision', exact: true}).all()) {
      await expect(link).toHaveAttribute('href', /^https:\/\/github\.com\/ArduPilot\/ardupilot_wiki\/blob\/[a-f0-9]{40}\//);
    }
    await expect(library.getByRole('button', {name: 'Check official sources', exact: true})).toBeDisabled();
    await library.getByRole('textbox', {name: 'Search saved passages', exact: true}).fill('PRIVATE_AIRCRAFT_SEARCH_ONLY');
    await library.getByRole('button', {name: 'Search local library', exact: true}).click();
    await expect(library.getByRole('heading', {name: 'No matching saved passages', exact: true})).toBeVisible();
    expect(engine.calls.some(call => call.method === 'research.refresh')).toBe(false);
    expect(engine.calls.filter(call => call.method === 'research.query').at(-1)?.params).toEqual({topic: 'vibration', query: 'PRIVATE_AIRCRAFT_SEARCH_ONLY'});

    await library.getByRole('combobox', {name: 'Performance research topic', exact: true}).selectOption('bidirectional_dshot');
    await expect(library.getByRole('textbox', {name: 'Search saved passages', exact: true})).toHaveValue('');
    await expect(library.getByRole('heading', {name: 'ESC telemetry and bidirectional DShot', exact: true, level: 2})).toBeVisible();
    await expect(library).toContainText('pole count');
    await expect(library).toContainText('firmware');
    await library.getByRole('checkbox', {name: 'Use an internet connection to check official sources for this topic', exact: true}).check();
    await library.getByRole('button', {name: 'Check official sources', exact: true}).click();
    await expect(page.getByText('Official source check unavailable in this offline test.', {exact: true})).toBeVisible();
    expect(engine.calls.filter(call => call.method === 'research.refresh').map(call => call.params)).toEqual([{topic: 'bidirectional_dshot', allow_online: true}]);
    await expect(library.getByText('Bundled reviewed excerpt', {exact: true})).toHaveCount(2);
    expect(engine.calls.some(call => /^(?:session\.prepare_and_watch|acquisition\.(?:parameters|logs)\.|network\.apply)/.test(call.method))).toBe(false);
  } finally { await engine.close(); }
});

test('downloaded source revisions reopen locally as unreviewed evidence without changing bundled advice', async ({page}, testInfo) => {
  const engine = await connectEngine(page);
  try {
    await engine.rpc('handshake'); // Finish the engine's initial catalog migration before fixture writes.
    // Exercise the real cache writer with deterministic HTTPS response bytes.
    // Only the external transport is replaced; no public network is contacted.
    const python = process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python';
    const {stdout} = await promisify(execFile)(resolve(python), ['-c', `
import json, sys
from pathlib import Path
from q22_engine.research import ResearchService, REF_URL
from q22_engine.store import Store
revision = 'a' * 40
def fetch(url, **limits):
    if url == REF_URL:
        return json.dumps({'object': {'type': 'commit', 'sha': revision}}).encode()
    if '/git/commits/' in url:
        return json.dumps({'sha': revision, 'committer': {'date': '2026-10-15T10:00:00Z'}}).encode()
    return b'Official synthetic source fixture\\nMotor RPM telemetry tracks the notch.\\nCompatible ESC firmware and pole count matter.\\n'
result = ResearchService(Store(Path(sys.argv[1])), fetcher=fetch).dispatch('research.refresh', {'topic': 'bidirectional_dshot', 'allow_online': True})
print(json.dumps({'state': result['state'], 'revision': result['check']['revision']}))
`, engine.data]);
    expect(JSON.parse(stdout)).toEqual({state: 'updated', revision: 'a'.repeat(40)});
    await page.goto('/');
    await expect(page.getByText('Engine connected', {exact: true})).toBeVisible();
    await page.getByRole('navigation').getByRole('button', {name: 'Research library', exact: true}).click();
    const library = page.locator('.research-workspace');
    await library.getByRole('combobox', {name: 'Performance research topic', exact: true}).selectOption('bidirectional_dshot');
    await expect(library.getByText('New source · review required', {exact: true})).toHaveCount(2);
    await expect(library).toContainText('The source differs from the bundled guidance revision.');
    await expect(library).toContainText('Motor RPM telemetry tracks the notch.');
    await expect(library).toContainText('Last saved check: Updated');
    for (const link of await library.getByRole('link', {name: 'Open this exact official source revision', exact: true}).all()) {
      await expect(link).toHaveAttribute('href', new RegExp(`/blob/${'a'.repeat(40)}/`));
    }
    await library.locator('.research-source').first().getByText('Source integrity and retrieval', {exact: true}).click();
    await expect(library.locator('.research-source').first()).toContainText('2026-10-15T10:00:00+00:00');
    expect(engine.calls.some(call => call.method === 'research.refresh')).toBe(false);
    await library.screenshot({path: testInfo.outputPath('local-research-review.png')});
    // Switching to an untouched topic still uses its pinned bundled revision.
    await library.getByRole('combobox', {name: 'Performance research topic', exact: true}).selectOption('vibration');
    await expect(library.getByText('Bundled reviewed excerpt', {exact: true})).toHaveCount(3);
    await expect(library.getByText('New source · review required', {exact: true})).toHaveCount(0);
  } finally { await engine.close(); }
});
