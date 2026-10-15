import { expect, type Page } from '@playwright/test';

// Legacy feature coverage remains reachable through the simplified product
// navigation. Keep this interaction-based: no route injection or hidden UI.
type Workspace = 'Overview' | 'Connection setup' | 'Prepare & watch' |
  'Network checks' | 'Evidence' | 'Flight insights' | 'Compare' |
  'Flight experiments' | 'Research library';

export async function openDisclosure(page: Page, name: string) {
  const summary = page.locator('summary').filter({hasText: new RegExp(`^${name}$`)});
  await expect(summary).toHaveCount(1);
  const details = summary.locator('..');
  if (await details.getAttribute('open') === null) await summary.click();
}

export async function openWorkspace(page: Page, workspace: Workspace) {
  const setup = workspace === 'Connection setup' || workspace === 'Network checks';
  await page.getByRole('navigation', {name: 'Main navigation'})
    .getByRole('button', {name: setup ? 'Setup' : 'Flights', exact: true}).click();
  if (workspace === 'Connection setup' || workspace === 'Overview') return;
  if (workspace === 'Prepare & watch') {
    await openDisclosure(page, 'Collection details');
    return;
  }
  if (workspace === 'Network checks') {
    await openDisclosure(page, 'Connection troubleshooting');
    return;
  }
  const advancedName = {
    Evidence: 'Import & manage sources',
    'Flight insights': 'Manual analysis',
    Compare: 'Manual comparison',
    'Flight experiments': 'Flight experiments',
    'Research library': 'Research library',
  }[workspace];
  await openDisclosure(page, 'Advanced tools');
  await page.getByRole('button', {name: advancedName, exact: true}).click();
}
