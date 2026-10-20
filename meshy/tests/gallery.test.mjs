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

// Image actions send explicit requests; they never implicitly approve unknown costs.
const { imageNextActions } = await import('../src/gallery.ts')
test('image Next uses task IDs and requires options/cost approval',()=>{
 const rows=imageNextActions('image-task',{kind:'remote',url:'https://assets.meshy.ai/a.png',label:'Mech',media:'image'})
 assert.equal(rows.length,3)
 assert.ok(rows[0].message.includes('meshy_image_to_3d'))
 assert.ok(rows.every(r=>r.message.includes('input_task_id "image-task"')&&r.message.includes('ask for my approval')))
})
test('saved image Next uses local files rather than an expired task reference',()=>{
 const rows=imageNextActions('old-task',{kind:'file',path:'/project/front.png',media:'image',images:['/project/front.png','/project/back.png']})
 assert.ok(rows[0].message.includes('meshy_image_to_3d'))
 assert.ok(rows.at(-1).message.includes('meshy_multi_image_to_3d'))
 assert.ok(rows[0].message.includes('/project/front.png')&&!rows[0].message.includes('/project/back.png'))
 assert.ok(rows.every(r=>!r.message.includes('Use input_task_id')))
})

test('image actions reference the displayed view and reserve all views for an explicit action',()=>{
 const source={kind:'remote',url:'https://assets.meshy.ai/front.png',images:['https://assets.meshy.ai/front.png','https://assets.meshy.ai/back.png'],media:'image',label:'Mech'}
 const rows=imageNextActions('task',source,1)
 assert.ok(rows[0].message.includes('only image 2 of 2'))
 assert.ok(rows[1].message.includes('only image 2 of 2'))
 assert.equal(rows.at(-1).label,'Make 3D from all views')
 assert.ok(rows.at(-1).message.includes('input_task_id "task"'))
})

test('opening a remote gallery source retains its task ID for workspace actions',()=>{
 const source=sourceForAsset(model)
 assert.equal(source.task,model.task_id)
})
