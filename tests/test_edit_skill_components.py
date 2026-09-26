"""Development-only browser tests; never injected into production Skill context."""
from pathlib import Path
import threading
import http.server
import pytest
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]/'.agents/skills'

@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as p:
        binary=Path(p.chromium.executable_path)
        browser=p.chromium.launch(headless=True,**({} if binary.is_file() else {'channel':'chrome'}))
        yield browser
        browser.close()

@pytest.fixture
def page(browser):
    context=browser.new_context(viewport={'width':900,'height':650},reduced_motion='no-preference')
    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
    page.set_content('<main><h1 id="existing">Existing product</h1><div id="mount"></div></main>')
    yield page
    context.close()
    assert not errors,errors

def load(page,slug):
    for file in sorted((ROOT/f'webcompass-{slug}'/'references').glob('*')):
        if file.suffix=='.js':page.add_script_tag(path=str(file))
        if file.suffix=='.css':page.add_style_tag(path=str(file))


def test_infinite_scroll_initial_load_custom_skeleton_and_end_label(page):
    load(page, 'infinite-scroll')
    page.evaluate('''() => {
      document.querySelector('#mount').style.marginTop='2000px';
      window.component=mountInfiniteScroll({container:document.querySelector('#mount'),initialLoad:true,
        endText:'Finished',getId:r=>r.id,
        renderLoading:()=>{const el=document.createElement('i');el.className='skeleton';return el;},
        loadPage:()=>new Promise(resolve=>window.finish=()=>resolve({items:[{id:1}],hasMore:false})),
        renderItem:()=>{const el=document.createElement('article');el.textContent='record';return el;}});
    }''')
    expect(page.locator('.skeleton')).to_have_count(1)
    page.evaluate('finish()')
    expect(page.locator('#mount article')).to_have_count(1)
    expect(page.locator('[role=status]')).to_have_text('Finished')
    expect(page.locator('.skeleton')).to_have_count(0)


def test_data_table_operations_share_one_dataset(page):
    load(page,'data-table')
    page.evaluate('''() => {window.selected=[];window.component=mountDataTable({container:document.querySelector('#mount'),
      rows:[{id:'a',name:'Alpha',score:30},{id:'b',name:'Beta',score:10},{id:'c',name:'Gamma',score:20}],
      columns:[{key:'name',label:'Name'},{key:'score',label:'Score'}],getId:r=>r.id,pageSize:2,selectable:true,onSelection:ids=>selected=ids});}''')
    page.get_by_role('button',name='Score',exact=True).click()
    assert page.locator('tbody tr').evaluate_all('(rows)=>rows.map(r=>r.dataset.rowId)')==['b','c']
    page.get_by_label('Select Beta').check()
    page.get_by_role('button',name='Next',exact=True).click()
    expect(page.locator('tbody')).to_contain_text('Alpha')
    page.get_by_label('Filter records').fill('no result')
    expect(page.locator('tbody')).to_contain_text('No matching records')
    page.get_by_label('Filter records').fill('')
    page.evaluate('component.setRows([])')
    assert page.evaluate('selected')==[]
    page.evaluate("component.setRows([{id:'a',name:'Alpha',score:30}])")
    page.set_viewport_size({'width':375,'height':650})
    assert page.locator('tbody tr').evaluate('(e)=>getComputedStyle(e).display')=='block'
    assert page.locator('tbody tr').bounding_box()['width']<=375


def test_data_table_optional_filters_edit_selection_and_bulk_action(page):
    load(page,'data-table')
    page.evaluate('''() => {window.events=[];window.rows=[
      {id:'a',name:'Alpha',kind:'Food',delta:2},{id:'b',name:'Beta',kind:'Sleep',delta:-3}];
      window.component=mountDataTable({container:document.querySelector('#mount'),rows,
        columns:[{key:'name',label:'Name',filter:'text',editable:true},
          {key:'kind',label:'Kind',filter:'select',filterOptions:['Food','Sleep']},
          {key:'delta',label:'Delta',filter:'sign'}],getId:r=>r.id,pageSize:1,pageSizes:[1,2],selectable:true,
        bulkActions:[{id:'archive',label:'Archive'}],onSelection:ids=>events.push(['selection',ids]),
        onEdit:change=>{events.push(['edit',change.id,change.value]);rows=rows.map(r=>r.id===change.id?{...r,name:change.value}:r);component.setRows(rows)},
        onBulkAction:(action,ids)=>events.push(['bulk',action,ids])});}''')
    page.get_by_label('Filter Kind').select_option('Sleep')
    expect(page.locator('tbody')).to_contain_text('Beta')
    page.get_by_label('Filter Kind').select_option('')
    page.get_by_label('Edit Name for Alpha').click()
    page.locator('tbody input[type=text]').fill('Updated')
    page.get_by_role('button',name='Save',exact=True).click()
    expect(page.locator('tbody')).to_contain_text('Updated')
    page.get_by_label('Select visible records').check()
    page.get_by_role('button',name='Archive',exact=True).click()
    assert page.evaluate("events.some(e=>e[0]==='bulk'&&e[1]==='archive'&&e[2][0]==='a')")
    page.get_by_label('Rows per page').select_option('2')
    assert page.evaluate('component.snapshot().pageSize')==2


def test_editor_real_selection_serialization_and_safe_paste(page):
    load(page,'rich-text-editor')
    page.evaluate('''() => {const output=document.createElement('input');output.type='hidden';output.id='output';document.body.append(output);
      window.component=mountRichTextEditor({container:document.querySelector('#mount'),initialHTML:'<p>Hello world</p>',output});
      const text=document.querySelector('.wc-editor__content p').firstChild,r=document.createRange();r.setStart(text,0);r.setEnd(text,5);getSelection().removeAllRanges();getSelection().addRange(r);}''')
    page.get_by_role('button',name='Bold',exact=True).click()
    expect(page.locator('.wc-editor__content strong')).to_have_text('Hello')
    assert '<strong>Hello</strong>' in page.locator('#output').input_value()
    page.get_by_role('button',name='Link',exact=True).click()
    page.get_by_label('Link or image URL').fill('https://example.org/article')
    page.get_by_role('button',name='Apply',exact=True).click()
    expect(page.locator('.wc-editor__content a')).to_have_attribute('href','https://example.org/article')
    page.evaluate(r'''() => {const editor=document.querySelector('.wc-editor__content');editor.focus();const r=document.createRange();r.selectNodeContents(editor);r.collapse(false);getSelection().removeAllRanges();getSelection().addRange(r);
      const data=new DataTransfer();data.setData('text/html','<p onclick="window.bad=1">Safe<script>window.bad=1<\/script></p>');
      editor.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));}''')
    assert page.evaluate('window.bad') is None
    value=page.locator('#output').input_value()
    assert 'onclick' not in value and '<script' not in value and 'Safe' in value


def test_drag_drop_moves_real_dom_and_emits_order(page):
    load(page,'drag-drop')
    page.evaluate('''() => {document.querySelector('#mount').innerHTML='<div id="list"><div data-id="a">A</div><div data-id="b">B</div></div>';
      for(const node of document.querySelectorAll('[data-id]'))node.style.cssText='height:80px;border:1px solid;padding:10px';
      window.order=[];window.component=bindDragDrop({lists:[{id:'main',element:document.querySelector('#list')}],itemSelector:'[data-id]',getId:el=>el.dataset.id,onChange:value=>order=value});}''')
    page.locator('[data-id=a]').drag_to(page.locator('[data-id=b]'),target_position={'x':30,'y':95})
    assert page.locator('#list>[data-id]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.id)')==['b','a']
    assert page.evaluate('order[0].items')==['b','a']
    page.evaluate('component.destroy()')
    assert page.locator('[data-id=a]').get_attribute('draggable') is None


def test_tree_parent_selection_partial_and_search(page):
    load(page,'tree-view')
    page.evaluate('''() => {window.component=mountTreeView({container:document.querySelector('#mount'),nodes:[{id:'root',label:'Root',children:[{id:'a',label:'Alpha'},{id:'b',label:'Beta'}]}]});}''')
    page.get_by_label('Select Root',exact=True).check()
    page.get_by_role('button',name='Expand Root',exact=True).click()
    expect(page.get_by_label('Select Alpha',exact=True)).to_be_checked()
    page.get_by_label('Select Alpha',exact=True).uncheck()
    assert page.get_by_label('Select Root',exact=True).evaluate('(node)=>node.indeterminate')
    assert page.evaluate('component.snapshot().selected')==['b']
    page.get_by_role('button',name='Collapse Root',exact=True).click()
    page.get_by_label('Search tree').fill('Beta')
    expect(page.get_by_label('Select Beta',exact=True)).to_be_visible()
    page.get_by_label('Search tree').fill('')
    expect(page.get_by_label('Select Beta',exact=True)).to_have_count(0)


def test_tree_lazy_children_are_indexed_once_and_locked_nodes_stay_unselected(page):
    load(page,'tree-view')
    page.evaluate('''() => {window.focused=[];window.component=mountTreeView({container:document.querySelector('#mount'),
      nodes:[{id:'root',label:'Root',loadChildren:()=>new Promise(resolve=>window.finishTree=resolve)}],
      onNodeFocus:node=>focused.push(node.id)});}''')
    page.get_by_role('button',name='Expand Root',exact=True).click()
    expect(page.get_by_text('Loading…',exact=True)).to_be_visible()
    page.evaluate("finishTree([{id:'open',label:'Open'},{id:'locked',label:'Locked',disabled:true}])")
    expect(page.get_by_label('Select Open',exact=True)).to_be_visible()
    expect(page.get_by_label('Select Locked',exact=True)).to_be_disabled()
    page.get_by_label('Select Open',exact=True).check()
    assert page.evaluate('component.snapshot().selected')==['open']
    assert page.evaluate('component.snapshot().loaded')==['root']
    page.get_by_text('Open',exact=True).click()
    assert page.evaluate('focused.at(-1)')=='open'


def test_dashboard_numbers_and_svg_share_samples(page):
    load(page,'realtime-dashboard')
    page.evaluate('''() => {let tick=0;window.component=mountRealtimeDashboard({container:document.querySelector('#mount'),metrics:[{key:'value',label:'Value'}],intervalMs:50,fetchSample:async()=>({value:++tick})});}''')
    page.wait_for_function('component.snapshot().sample.value >= 3')
    sample=page.evaluate('component.snapshot()')
    assert sample['series']['value'][-1]==sample['sample']['value']
    assert len(page.locator('polyline').get_attribute('points').split())>=3
    page.evaluate('component.destroy()')
    expect(page.locator('.wc-dashboard')).to_have_count(0)


def test_infinite_scroll_underfilled_viewport_and_dedup(page):
    load(page,'infinite-scroll')
    page.evaluate('''() => {window.calls=[];window.component=mountInfiniteScroll({container:document.querySelector('#mount'),getId:r=>r.id,
      loadPage:async p=>{calls.push(p);return {items:p===1?[{id:1}]:[{id:p-1},{id:p}],hasMore:p<3};},
      renderItem:item=>{const node=document.createElement('div');node.dataset.item=item.id;node.textContent='Record '+item.id;return node;}});}''')
    expect(page.get_by_text('End of content',exact=True)).to_be_visible()
    assert page.evaluate('calls')==[1,2,3]
    assert page.locator('[data-item]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.item)')==['1','2','3']
    page.evaluate('component.load()')
    assert page.evaluate('calls')==[1,2,3]


def test_async_validation_stale_response_and_native_constraints(page):
    load(page,'async-validation')
    page.evaluate('''() => {document.querySelector('#mount').innerHTML='<form><input id="name" required><input id="other" required><button>Submit</button></form>';
      window.pending={};window.component=bindAsyncValidation({form:document.querySelector('form'),input:document.querySelector('#name'),submitButton:document.querySelector('button'),debounceMs:0,
      check:value=>new Promise(resolve=>pending[value]=resolve)});}''')
    page.locator('#name').fill('older');page.wait_for_function('!!pending.older')
    page.locator('#name').fill('newer');page.wait_for_function('!!pending.newer')
    page.evaluate("pending.newer({valid:true});pending.older({valid:false,message:'Stale error'})")
    expect(page.get_by_role('button',name='Submit')).to_be_disabled()
    page.locator('#other').fill('valid')
    expect(page.get_by_role('button',name='Submit')).to_be_enabled()
    assert page.evaluate('component.snapshot().status')=='valid'


def test_upload_queue_real_files_cancel_and_progress(page):
    load(page,'file-upload')
    page.evaluate('''() => {window.component=mountFileUpload({container:document.querySelector('#mount'),accept:'.txt',concurrency:1,
      upload:(file,{signal,onProgress})=>new Promise((resolve,reject)=>{let progress=0;const timer=setInterval(()=>{progress+=.2;onProgress(progress);if(progress>=1){clearInterval(timer);resolve('done');}},80);
      signal.addEventListener('abort',()=>{clearInterval(timer);reject(new DOMException('Aborted','AbortError'));},{once:true});})});}''')
    page.get_by_label('Choose files').set_input_files([{'name':'first.txt','mimeType':'text/plain','buffer':b'first'},{'name':'second.txt','mimeType':'text/plain','buffer':b'second'}])
    page.wait_for_function('component.snapshot()[0].progress > 0')
    page.get_by_role('button',name='Cancel',exact=True).first.click()
    page.wait_for_function("component.snapshot()[1].state === 'complete'")
    result=page.evaluate('component.snapshot()')
    assert result[0]['state']=='cancelled' and result[0]['progress']<1
    assert result[1]['progress']==1
    expect(page.locator('.wc-upload li').nth(1)).to_contain_text('100%')
    page.get_by_label('Choose files').set_input_files({'name':'bad.exe','mimeType':'application/octet-stream','buffer':b'bad'})
    expect(page.get_by_role('alert')).to_contain_text('Rejected bad.exe')


def test_upload_optional_features_reuse_one_queue_and_release_previews(page):
    load(page, 'file-upload')
    page.evaluate('''() => {
      window.revoked=[]; const revoke=URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL=url=>{revoked.push(url);revoke(url)};
      window.component=mountFileUpload({container:document.querySelector('#mount'),accept:'image/*,.txt',
        concurrency:1,maxBytes:1024,deduplicate:true,removable:true,preview:true,
        onCreate:job=>job.row.dataset.file=job.file.name,
        upload:(file,{signal,onProgress})=>new Promise((resolve,reject)=>{
          onProgress(.4);signal.addEventListener('abort',()=>reject(new Error('cancelled')),{once:true});
        })});
    }''')
    svg={'name':'diagram.svg','mimeType':'image/svg+xml','buffer':b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'}
    note={'name':'notes.txt','mimeType':'text/plain','buffer':b'notes'}
    page.get_by_label('Choose files',exact=True).set_input_files([svg,note])
    page.wait_for_function('component.snapshot()[0].progress===.4')
    assert page.locator('[data-file="diagram.svg"] img').evaluate('(e)=>e.complete && e.naturalWidth') == 10
    page.get_by_label('Choose files',exact=True).set_input_files(svg)
    assert len(page.evaluate('component.snapshot()')) == 2
    expect(page.get_by_role('alert')).to_contain_text('Already queued')
    page.locator('[data-file="notes.txt"]').get_by_role('button',name='Remove',exact=True).click()
    assert len(page.evaluate('component.snapshot()')) == 1
    page.locator('[data-file="diagram.svg"]').get_by_role('button',name='Cancel',exact=True).click()
    page.locator('[data-file="diagram.svg"]').get_by_role('button',name='Remove',exact=True).click()
    assert page.evaluate('component.snapshot()') == []
    assert page.evaluate('revoked.length') == 1
    page.get_by_label('Choose files',exact=True).set_input_files({'name':'large.txt','mimeType':'text/plain','buffer':b'x'*1025})
    expect(page.get_by_role('alert')).to_contain_text('exceeds 1024 bytes')
    assert page.evaluate('component.snapshot()') == []


def test_parallax_different_layer_displacements_and_cleanup(page):
    load(page,'parallax')
    page.evaluate('''() => {document.querySelector('#mount').innerHTML='<section id="scene" style="margin-top:200px;height:900px"><div id="back">Back</div><div id="front">Front</div></section><div style="height:1500px"></div>';
      window.component=bindParallax({section:document.querySelector('#scene'),layers:[{element:document.querySelector('#back'),speed:.2},{element:document.querySelector('#front'),speed:.8}]});}''')
    page.wait_for_function("document.querySelector('#back').style.transform.includes('translate3d')")
    before=page.locator('#back').evaluate('(e)=>e.style.transform')
    page.evaluate('scrollTo(0,400)')
    page.wait_for_function('(before)=>document.querySelector("#back").style.transform!==before',arg=before)
    offsets=page.evaluate("['back','front'].map(id=>new DOMMatrix(getComputedStyle(document.getElementById(id)).transform).m42)")
    assert offsets[0]!=offsets[1]
    page.evaluate('component.destroy()')
    assert page.locator('#back').evaluate('(e)=>e.style.transform')==''


def test_page_transition_animation_final_state_and_back(page):
    load(page,'page-transitions')
    page.evaluate('''() => {document.querySelector('#mount').innerHTML='<div id="stage"><section id="a"><button>A</button></section><section id="b"><button>B</button></section></div>';
      window.component=bindPageTransitions({views:{a:document.querySelector('#a'),b:document.querySelector('#b')},initial:'a',duration:150,historyKey:'test-view'});component.go('b');}''')
    assert page.evaluate('component.snapshot().busy')
    assert page.locator('#a').evaluate('(e)=>e.inert')
    assert page.locator('#b').evaluate('(e)=>e.inert')
    page.wait_for_function("component.snapshot().active==='b' && !component.snapshot().busy")
    expect(page.locator('#a')).to_be_hidden();expect(page.locator('#b')).to_be_visible()
    assert page.locator('#b').evaluate('(e)=>e.style.position')==''
    page.evaluate('history.back()')
    page.wait_for_function("component.snapshot().active==='a' && !component.snapshot().busy")
    expect(page.locator('#b')).to_be_hidden()


def test_particles_render_motion_input_and_cleanup(page):
    load(page,'particles')
    page.evaluate('''() => {const host=document.querySelector('#mount');host.style.cssText='height:240px;width:400px';window.component=mountParticles({container:host,count:20});}''')
    page.wait_for_function('document.querySelector("canvas").width===400')
    first=page.locator('canvas').evaluate('(c)=>c.toDataURL()')
    page.wait_for_function('(first)=>document.querySelector("canvas").toDataURL()!==first',arg=first)
    box=page.locator('canvas').bounding_box();page.mouse.move(box['x']+150,box['y']+100);page.mouse.click(box['x']+180,box['y']+120)
    assert page.locator('canvas').evaluate('(c)=>getComputedStyle(c).pointerEvents')=='none'
    page.evaluate('component.destroy()');expect(page.locator('canvas')).to_have_count(0)


def test_skeleton_pending_content_and_dimensions(page):
    load(page,'skeleton-loading')
    page.evaluate('''() => {const skeleton=document.createElement('article');skeleton.style.height='120px';skeleton.innerHTML='<div data-skeleton-block style="height:30px"></div><div data-skeleton-block style="height:70px"></div>';
      window.component=mountSkeletonLoading({container:document.querySelector('#mount'),skeleton,load:()=>new Promise(resolve=>window.loaded=resolve),renderContent:data=>{const node=document.createElement('article');node.style.height='120px';node.textContent=data;return node;}});}''')
    expect(page.locator('.wc-skeleton')).to_be_visible()
    height=page.locator('.wc-skeleton').bounding_box()['height']
    expect(page.locator('#mount')).to_have_attribute('aria-busy','true')
    page.evaluate("loaded('Loaded story')")
    expect(page.get_by_text('Loaded story',exact=True)).to_be_visible()
    expect(page.locator('.wc-skeleton')).to_have_count(0)
    expect(page.locator('#mount')).to_have_attribute('aria-busy','false')
    assert page.get_by_text('Loaded story',exact=True).bounding_box()['height']==height


def test_authentication_error_success_logout_without_password_storage(page):
    load(page,'authentication')
    page.evaluate('''() => {window.calls=0;window.session=null;window.component=mountAuthentication({container:document.querySelector('#mount'),
      authenticate:async credentials=>{calls++;if(credentials.password!=='correct')throw Error('Wrong password');return {name:'Reader',email:credentials.email};},
      signOut:async()=>{},onSession:user=>session=user});}''')
    page.get_by_label('Email',exact=True).fill('reader@example.test')
    page.get_by_label('Password',exact=True).fill('wrong')
    page.get_by_role('button',name='Log in',exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('Wrong password')
    page.get_by_label('Password',exact=True).fill('correct')
    page.get_by_role('button',name='Log in',exact=True).click()
    expect(page.get_by_text('Signed in as Reader',exact=True)).to_be_visible()
    assert page.evaluate('session.name')=='Reader'
    page.get_by_role('button',name='Log out',exact=True).click()
    expect(page.get_by_text('Not signed in',exact=True)).to_be_visible()
    assert page.evaluate('session') is None


def test_wizard_retains_fields_and_rechecks_prior_steps(page):
    load(page,'wizard')
    page.evaluate('''() => {document.querySelector('#mount').innerHTML='<form><section id="one"><input aria-label="Name" name="name" required></section><section id="two"><input aria-label="Company" name="company" required></section></form>';
      window.completed=null;window.component=mountWizard({form:document.querySelector('form'),steps:[{element:document.querySelector('#one'),label:'Personal'},{element:document.querySelector('#two'),label:'Company'}],onComplete:async data=>completed=data});}''')
    expect(page.get_by_role('button',name='Next',exact=True)).to_be_disabled()
    page.get_by_label('Name',exact=True).fill('Reader');page.get_by_role('button',name='Next',exact=True).click()
    page.get_by_label('Company',exact=True).fill('Workshop')
    page.get_by_role('button',name='Back',exact=True).click();expect(page.get_by_label('Name',exact=True)).to_have_value('Reader')
    page.get_by_role('button',name='Next',exact=True).click()
    page.evaluate("document.querySelector('[name=name]').value=''")
    page.get_by_role('button',name='Finish',exact=True).click()
    expect(page.get_by_label('Name',exact=True)).to_be_visible();assert page.evaluate('completed') is None
    page.get_by_label('Name',exact=True).fill('Reader');page.get_by_role('button',name='Next',exact=True).click()
    expect(page.get_by_label('Company',exact=True)).to_have_value('Workshop')
    page.get_by_role('button',name='Finish',exact=True).click()
    expect(page.get_by_role('status')).to_have_text('Completed')
    assert page.evaluate('completed')=={'name':'Reader','company':'Workshop'}


def test_notifications_unread_actions_delete_and_new_arrivals(page):
    load(page,'notification-center')
    page.evaluate('''() => {window.action=null;window.component=mountNotificationCenter({container:document.querySelector('#mount'),notifications:[{id:'a',message:'First'},{id:'b',message:'Second',actionLabel:'Open'}],allowDelete:true,onAction:n=>action=n.id});}''')
    expect(page.locator('output')).to_have_text('2 unread')
    page.locator('[data-notification-id=a]').get_by_role('button',name='Mark as read').click()
    expect(page.locator('output')).to_have_text('1 unread')
    page.get_by_role('button',name='Open',exact=True).click();assert page.evaluate('action')=='b'
    expect(page.locator('output')).to_have_text('0 unread')
    page.evaluate("component.push({id:'c',message:'Third'})")
    expect(page.locator('output')).to_have_text('1 unread')
    page.locator('[data-notification-id=c]').get_by_role('button',name='Delete').click()
    expect(page.locator('output')).to_have_text('0 unread')


def test_editor_cross_paragraph_format_and_list_conversion(page):
    load(page,'rich-text-editor')
    page.evaluate("""() => {
      window.component=mountRichTextEditor({container:document.querySelector('#mount'),initialHTML:'<p>abc</p><p>def</p>'});
      const p=document.querySelectorAll('.wc-editor__content p'),r=document.createRange();
      r.setStart(p[0].firstChild,1);r.setEnd(p[1].firstChild,2);getSelection().removeAllRanges();getSelection().addRange(r);
    }""")
    page.get_by_role('button',name='Bold',exact=True).click()
    assert page.evaluate('component.getHTML()')=='<p>a<strong>bc</strong></p><p><strong>de</strong>f</p>'
    page.evaluate("""() => {
      component.destroy();window.component=mountRichTextEditor({container:document.querySelector('#mount'),initialHTML:'<ul><li>Alpha</li><li>Beta</li></ul>'});
      const r=document.createRange();r.selectNodeContents(document.querySelector('.wc-editor__content'));getSelection().removeAllRanges();getSelection().addRange(r);
    }""")
    page.get_by_role('button',name='Heading 1',exact=True).click()
    assert page.evaluate('component.getHTML()')=='<h1>Alpha</h1><h1>Beta</h1>'
    page.get_by_role('button',name='Numbered list',exact=True).click()
    assert page.evaluate('component.getHTML()')=='<ol><li>Alpha</li><li>Beta</li></ol>'


def test_upload_uses_real_http_multipart_transport(page):
    received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<div id="mount"></div>')
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(201);self.end_headers();self.wfile.write(b'uploaded')
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        page.goto(f'http://127.0.0.1:{server.server_port}/')
        load(page,'file-upload')
        page.evaluate("window.component=mountFileUpload({container:document.querySelector('#mount'),upload:createXHRUpload('/upload')})")
        page.get_by_label('Choose files').set_input_files({'name':'real.txt','mimeType':'text/plain','buffer':b'actual request body'})
        page.wait_for_function("component.snapshot()[0]?.state === 'complete'")
        assert len(received)==1 and b'actual request body' in received[0] and b'filename="real.txt"' in received[0]
        assert page.get_by_role('progressbar').evaluate('(e)=>e.value')==1
    finally:
        server.shutdown();server.server_close();thread.join()


def test_transition_browser_back_during_animation_keeps_history_consistent(page):
    load(page,'page-transitions')
    page.evaluate("""() => {
      document.querySelector('#mount').innerHTML='<section id="a">A</section><section id="b">B</section><section id="c">C</section>';
      window.component=bindPageTransitions({views:{a:document.querySelector('#a'),b:document.querySelector('#b'),c:document.querySelector('#c')},initial:'a',duration:100,historyKey:'view'});
      component.go('b');
    }""")
    page.wait_for_function("component.snapshot().active==='b' && !component.snapshot().busy")
    page.evaluate("() => {component.go('c');history.back();}")
    page.wait_for_function("component.snapshot().active==='a' && !component.snapshot().busy")
    assert page.evaluate('history.state.view')=='a'


def test_drag_drop_keyboard_cross_list_preserves_existing_nodes(page):
    load(page, 'drag-drop')
    page.evaluate('''() => {
      document.querySelector('#mount').innerHTML='<div id="active"><article id="original" data-id="a"><progress value="35" max="100"></progress>Mission</article></div><div id="done"></div>';
      window.original=document.querySelector('#original'); window.orders=[];
      window.component=bindDragDrop({lists:[{id:'active',element:document.querySelector('#active')},{id:'done',element:document.querySelector('#done')}],itemSelector:'[data-id]',getId:e=>e.dataset.id,onChange:x=>orders.push(x)});
    }''')
    page.locator('#original').press('Alt+ArrowRight')
    assert page.locator('#done #original progress').evaluate('(e)=>e.value') == 35
    assert page.evaluate('document.querySelector("#done #original") === original')
    assert page.evaluate('orders.at(-1)') == [{'id':'active','items':[]},{'id':'done','items':['a']}]
    page.locator('#original').press('Alt+ArrowLeft')
    expect(page.locator('#active #original')).to_be_focused()
    page.evaluate('component.destroy()')
    assert page.locator('#original').get_attribute('draggable') is None


def test_notifications_expose_shared_state_and_metadata_hooks(page):
    load(page, 'notification-center')
    page.evaluate('''() => {
      window.updates=[];
      window.component=mountNotificationCenter({container:document.querySelector('#mount'),
        notifications:[{id:'upload-1',message:'Report uploaded',kind:'success',title:'Campaign record',read:false}],
        onChange:(entries,unread)=>updates.push({entries,unread}),
        onCreateRow:(row,entry)=>{row.dataset.kind=entry.kind;const title=document.createElement('strong');title.textContent=entry.title;row.prepend(title)}
      });
    }''')
    expect(page.locator('[data-notification-id="upload-1"] strong')).to_have_text('Campaign record')
    assert page.evaluate('updates.at(-1).unread') == 1
    page.get_by_role('button',name='Mark as read',exact=True).click()
    assert page.evaluate('updates.at(-1).unread') == 0
    assert page.evaluate('component.snapshot()[0].kind') == 'success'
    assert page.evaluate('component.snapshot()[0].read') is True


def test_simulated_upload_uses_json_policy_not_fixture_names(page):
    load(page,'file-upload')
    page.evaluate('''() => {window.component=mountFileUpload({container:document.querySelector('#mount'),upload:createSimulatedUpload({durationMs:350,tickMs:20})});}''')
    page.get_by_label('Choose files',exact=True).set_input_files([
        {'name':'unrelated-name.json','mimeType':'application/json','buffer':b'{"simulateFailure":true}'},
        {'name':'retry-report.json','mimeType':'application/json','buffer':b'{"simulateFailure":false}'}])
    page.wait_for_function("component.snapshot()[0].state==='failed' && component.snapshot()[1].state==='complete'")
    page.locator('.wc-upload li').first.get_by_role('button',name='Retry',exact=True).click()
    page.wait_for_function("component.snapshot().every(j=>j.state==='complete')")
    expect(page.locator('.wc-upload li').first).to_contain_text('100%')
    page.get_by_label('Choose files',exact=True).set_input_files({'name':'cancel.json','mimeType':'application/json','buffer':b'{}'})
    page.locator('.wc-upload li').last.get_by_role('button',name='Cancel',exact=True).click()
    page.wait_for_function("component.snapshot()[2].state==='cancelled'")


def test_editor_set_html_sanitizes_and_synchronizes_draft(page):
    load(page,'rich-text-editor')
    page.evaluate('''() => {const output=document.createElement('input');output.id='draft';document.body.append(output);
      window.component=mountRichTextEditor({container:document.querySelector('#mount'),output,onChange:value=>window.lastDraft=value});
      component.setHTML('<p>Restored <strong>note</strong><script>window.bad=1<\/script></p>');}''')
    expect(page.locator('.wc-editor__content strong')).to_have_text('note')
    assert '<script' not in page.locator('#draft').input_value()
    assert page.evaluate('lastDraft') == page.locator('#draft').input_value()
    page.evaluate('component.setHTML("")')
    assert page.locator('.wc-editor__content').inner_text().strip() == ''
    assert page.evaluate('component.getHTML()') == page.locator('#draft').input_value()


def test_drag_handle_invalid_drop_keeps_item_and_cleans_up(page):
    load(page, 'drag-drop')
    page.evaluate('''() => {
      document.querySelector('#mount').innerHTML = '<div id="left"><article data-id="a"><button class="handle">Move A</button></article></div><div id="right"></div><div id="outside">Outside</div>';
      for (const id of ['left', 'right', 'outside']) document.getElementById(id).style.cssText='min-height:90px;border:1px solid;padding:10px';
      window.invalid = 0;
      window.component = bindDragDrop({lists:[{id:'left',element:document.querySelector('#left')},{id:'right',element:document.querySelector('#right')}],itemSelector:'[data-id]',getId:item=>item.dataset.id,handleSelector:'.handle',onInvalidDrop:()=>invalid++});
    }''')
    page.locator('.handle').drag_to(page.locator('#right'))
    expect(page.locator('#right [data-id=a]')).to_have_count(1)
    page.locator('.handle').drag_to(page.locator('#outside'))
    expect(page.locator('#right [data-id=a]')).to_have_count(1)
    assert page.evaluate('invalid') == 1
    page.evaluate('component.destroy()')
    assert page.locator('.handle').get_attribute('draggable') is None


def test_editor_link_label_preserves_bold_and_image_failure_feedback(page):
    load(page, 'rich-text-editor')
    page.evaluate("window.component=mountRichTextEditor({container:document.querySelector('#mount')})")
    editor=page.locator('.wc-editor__content')
    editor.fill('Formatted label');editor.press('ControlOrMeta+a')
    page.get_by_role('button',name='Bold',exact=True).click()
    page.get_by_role('button',name='Link',exact=True).click()
    page.get_by_label('Link or image URL').fill('https://example.org/note')
    page.get_by_label('Link text (optional)').fill('Renamed link')
    page.get_by_role('button',name='Apply',exact=True).click()
    expect(editor.locator('strong a')).to_have_text('Renamed link')
    assert '<strong>' in page.evaluate('component.getHTML()')
    expect(page.locator('.wc-editor form')).to_be_hidden()
    page.get_by_role('button',name='Image',exact=True).click()
    page.route('https://example.org/missing.png',lambda route:route.abort())
    page.get_by_label('Link or image URL').fill('https://example.org/missing.png')
    page.get_by_role('button',name='Apply',exact=True).click()
    expect(editor.locator('img')).to_have_attribute('alt','Image unavailable')
    expect(page.locator('.wc-editor [role=alert]')).to_contain_text('could not be loaded')
