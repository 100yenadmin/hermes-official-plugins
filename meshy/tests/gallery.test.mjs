import assert from 'node:assert/strict'
import { test } from 'node:test'
import { matchesKind, sourceForAsset } from '../src/gallery.ts'

const model = { task_id: 'task', task_type: 'image-to-3d', prompt: 'Mech', kind: 'model',
  image_urls: [], expired: false, glb_url: 'https://assets.meshy.ai/mech.glb' }

for (const [name, asset, expected] of [
  ['model from an image remains a model', model, 'model'],
  ['concept image is not passed to the GLTF loader', { ...model, kind: 'image', image_urls: ['https://assets.meshy.ai/concept.png'] }, 'image'],
  ['saved model survives remote expiry', { ...model, expired: true, local_path: '/project/mech.glb' }, 'file'],
  ['remote expiry reaches the viewer without guessing from task age', { ...model, expired: true }, 'expired'],
  ['missing output has no selectable preview', { ...model, glb_url: null }, 'none']
]) {
  test(name, () => {
    const source = sourceForAsset(asset)
    const actual = !source ? 'none' : source.kind === 'file' ? 'file' : source.expired ? 'expired' : source.media
    assert.equal(actual, expected)
  })
}

test('image set keeps every view for navigation', () => {
  const images = ['https://assets.meshy.ai/front.png', 'https://assets.meshy.ai/back.png']
  assert.deepEqual(sourceForAsset({ ...model, kind: 'image', image_urls: images }).images, images)
})

for (const [filter, expected] of [['all', ['image', 'model']], ['image', ['image']], ['model', ['model']]]) {
  test(`${filter} filter selects the intended assets`, () => {
    assert.deepEqual(['image', 'model'].filter(kind => matchesKind(filter, kind)), expected)
  })
}
