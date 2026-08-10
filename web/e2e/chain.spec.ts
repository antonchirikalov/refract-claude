import { expect, test } from '@playwright/test'

import { element, node, watchForBreakage } from './helpers'

/**
 * A loop body may be a CHAIN of elements (SPEC §10.3). The container must show every
 * element, the inspector must address them individually, and the run must execute them
 * in order — the seeded `chain-project` has writer → fact-checker → critic.
 */

test('a container shows every body element plus its critic', async ({ page }) => {
  const problems = watchForBreakage(page)
  await page.goto('/#/projects/chain-project')

  const refine = node(page, 'refine')
  await expect(refine).toHaveClass(/is-container/)
  // three elements inside one container: two body steps and the single controller
  await expect(refine.locator('.gblock')).toHaveCount(3)
  await expect(element(page, 'requirements_writer')).toBeVisible()
  await expect(element(page, 'requirements_fact_checker')).toBeVisible()
  await expect(element(page, 'requirements_critic')).toBeVisible()
  expect(problems).toEqual([])
})

test('the inspector edits one chain element, not the whole body', async ({ page }) => {
  const problems = watchForBreakage(page)
  await page.goto('/#/projects/chain-project')

  await element(page, 'requirements_fact_checker').click()
  const panel = page.locator('.inspector')
  // its position in the chain is what a reader needs, not the raw block name
  await expect(panel.locator('.inspector-kind')).toHaveText('loop · body2')
  await expect(panel).toContainText('Body step 2')

  await panel.locator('select').selectOption('claude/opus')
  await expect(
    element(page, 'requirements_fact_checker').locator('.model-name'),
  ).toHaveText('opus')

  // it was written to the pipeline file, and ONLY for this element
  await page.reload()
  await expect(
    element(page, 'requirements_fact_checker').locator('.model-name'),
  ).toHaveText('opus')
  await expect(
    element(page, 'requirements_writer').locator('.model-name'),
  ).not.toHaveText('opus')
  expect(problems).toEqual([])
})

test('a chain runs element by element and the loop completes', async ({ page }) => {
  const problems = watchForBreakage(page)
  await page.goto('/#/projects/chain-project')
  await page.getByRole('button', { name: 'Run' }).click()
  await page.waitForURL(/runs\//)

  await expect(page.locator('.pill.is-completed')).toBeVisible({ timeout: 60_000 })
  await expect(node(page, 'refine').locator('.gnode-status')).toHaveText('done')

  // consecutive heartbeats of one step collapse to a single line, so a long step
  // cannot bury the state changes around it
  const feed = page.locator('.feed')
  await expect(feed).toContainText('refine.body2:r1')
  const beats = await feed.locator('li', { hasText: 'heartbeat' }).count()
  const steps = await feed.locator('li', { hasText: 'step_state_changed' }).count()
  expect(beats).toBeLessThanOrEqual(steps)
  expect(problems).toEqual([])
})
