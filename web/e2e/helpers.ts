import { resolve } from 'node:path'

import { expect, type Page } from '@playwright/test'

/** The repo's synthetic sources; the import endpoint copies from a real path. */
export const SAMPLE_DOCS = resolve(process.cwd(), '..', 'examples/extract-project/input')

/** One node by its exact id — "refine" must not also match "sd_refine". */
export function node(page: Page, id: string) {
  return page.locator('.gnode', {
    has: page.locator(`.gnode-id:text-is("${id}")`),
  })
}

/** One element inside a container, by its agent name. */
export function element(page: Page, agent: string) {
  return page.locator('.gblock', {
    has: page.locator(`.gblock-agent:text-is("${agent}")`),
  })
}

/** Fails the test on any console error or failed request the page produced. */
export function watchForBreakage(page: Page): string[] {
  const problems: string[] = []
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`))
  page.on('console', (m) => {
    // subresource failures arrive here without a URL; the response listener below
    // judges those, with the URL in hand
    if (m.type() === 'error' && !m.text().includes('Failed to load resource')) {
      problems.push(`console: ${m.text()}`)
    }
  })
  page.on('response', (r) => {
    // a fresh run's ledger appears a moment after the 202, and a provider logo is
    // probed once on purpose — neither is a defect
    const fresh404 = r.status() === 404 && /\/api\/runs\/run_[0-9_]+$/.test(r.url())
    const logo = r.status() === 404 && r.url().includes('/logos/')
    if (r.status() >= 400 && !fresh404 && !logo) {
      problems.push(`${r.status()} ${r.url()}`)
    }
  })
  return problems
}

/** A project created from a template, with the repo's synthetic documents copied in. */
export async function createProject(
  page: Page,
  template: string,
  name = `p-${Date.now().toString().slice(-7)}`,
): Promise<string> {
  await page.goto('/#/new')
  await page.getByLabel('Name').fill(name)
  await page.locator(`.card.selectable:has(h3:text-is("${template}"))`).click()
  const docs = page.getByLabel('Documents folder (copied into the project)')
  if (await docs.count()) {
    await docs.fill(SAMPLE_DOCS)
  }
  // pinned so a spec never depends on which provider happens to be first
  await page.getByLabel('Default model').selectOption('claude/sonnet')
  await page.getByRole('button', { name: 'Create project' }).click()
  await page.waitForURL(new RegExp(`#/projects/${name}`))
  await expect(page.locator('.gnode').first()).toBeVisible()
  return name
}

export async function runToCheckpoint(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Run' }).click()
  await page.waitForURL(/runs\//)
  await expect(page.locator('.panel.warn h3')).toContainText('Checkpoint')
}
