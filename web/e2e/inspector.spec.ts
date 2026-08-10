import { expect, test } from '@playwright/test'

import { createProject, element, node, watchForBreakage } from './helpers'

test('a container shows its own properties and an element shows its model', async ({
  page,
}) => {
  const problems = watchForBreakage(page)
  await createProject(page, 'requirements_to_design')

  // the container: rounds, what it reads, what it may use
  await node(page, 'refine').click()
  const panel = page.locator('.inspector')
  await expect(panel.locator('.inspector-kind')).toHaveText('loop')
  await expect(panel).toContainText('Rounds before it gives up')
  await expect(panel).toContainText('extract from extract')
  await expect(panel).toContainText('tavily-remote (external tool)') // plain words
  await expect(panel).toContainText('stops here and waits for your review')

  // an element inside it: role, agent, model picker
  await element(page, 'requirements_critic').click()
  await expect(panel.locator('.inspector-kind')).toHaveText('loop · critic')
  await expect(panel.getByRole('heading')).toHaveText('requirements_critic')
  await expect(panel.locator('select')).toHaveValue('claude/sonnet')
  expect(problems).toEqual([])
})

test('changing an element model is written and reflected in the graph', async ({
  page,
}) => {
  const problems = watchForBreakage(page)
  await createProject(page, 'requirements_to_design')

  await node(page, 'refine').click()
  await element(page, 'requirements_critic').click()
  await page.locator('.inspector select').selectOption('claude/opus')

  const criticCard = element(page, 'requirements_critic')
  await expect(criticCard.locator('.model-name')).toHaveText('opus')
  // and it survives a reload, i.e. it was written to the pipeline file
  await page.reload()
  await expect(
    element(page, 'requirements_critic').locator('.model-name'),
  ).toHaveText('opus')
  expect(problems).toEqual([])
})

test('a loop round count can be changed from the container', async ({ page }) => {
  const problems = watchForBreakage(page)
  await createProject(page, 'requirements_to_design')

  const loop = node(page, 'refine')
  await loop.click()
  await page.locator('.inspector input[type="number"]').fill('5')
  await page.locator('.inspector').getByRole('button', { name: 'Save' }).click()

  await expect(loop).toContainText('rounds ≤5')
  expect(problems).toEqual([])
})

test('an edit that would break the pipeline is refused with a reason', async ({
  page,
}) => {
  // The provider without a key must not be selectable into a valid pipeline: the
  // engine validates the whole graph before committing (SPEC §19.2.1).
  await createProject(page, 'requirements_to_design')
  await node(page, 'extract').click()

  const refused = await page.evaluate(async () => {
    const project = location.hash.split('/')[2]
    const resp = await fetch(
      `/api/projects/${project}/pipelines/requirements_to_design/nodes/extract`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: 'nosuchprovider/x' }),
      },
    )
    return { status: resp.status, body: await resp.json() }
  })

  expect(refused.status).toBe(409)
  expect(JSON.stringify(refused.body)).toContain('E_PROVIDER_UNAVAILABLE')
  // the graph still shows the old model
  await expect(
    node(page, 'extract').locator('.model-name'),
  ).toHaveText('sonnet')
})
