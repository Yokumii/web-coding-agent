from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from src.orchestration.edit_skills import (
    EDIT_SKILLS,
    SKILLS_ROOT,
    reference_destinations,
    render_edit_skill,
    selected_edit_skill,
)


SKILL = SKILLS_ROOT / "webcompass-shopping-cart"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_selection_uses_current_structured_type_only(tmp_path):
    assert selected_edit_skill({"task_type": "Shopping Cart"}) == "webcompass-shopping-cart"
    assert selected_edit_skill({"classification": {"primary": {
        "taxonomy": "webcompass", "type": "Shopping Cart"}}}) == "webcompass-shopping-cart"
    assert selected_edit_skill({"instruction": "Add a shopping cart"}) is None
    assert selected_edit_skill({"task_type": "Data Table", "previous": "Shopping Cart"}) == "webcompass-data-table"
    assert selected_edit_skill({"classification": {"primary": {
        "taxonomy": "extension", "type": "Shopping Cart"}}}) is None
    with pytest.raises(ValueError, match="conflicting"):
        selected_edit_skill({"task_type": "Data Table", "classification": {"primary": {
            "taxonomy": "webcompass", "type": "Shopping Cart"}}})
    (tmp_path / ".harness").mkdir()
    contract = tmp_path / ".harness/edit_task_contract.json"
    contract.write_text(json.dumps({"schema_version": "edit-task-contract-v1", "chain_metadata": {"task_type": "Shopping Cart"}}))
    prompt = render_edit_skill(tmp_path)
    assert ".harness/edit_skill/webcompass-shopping-cart/references/cart.js" in prompt
    staged = tmp_path / ".harness/edit_skill/webcompass-shopping-cart/references/cart.js"
    assert staged.read_bytes() == (SKILL / "references/cart.js").read_bytes()
    assert "function createCart(items)" in prompt
    assert "copy_from" in prompt
    for task_type, name in EDIT_SKILLS.items():
        contract.write_text(json.dumps({"schema_version": "edit-task-contract-v1", "chain_metadata": {"task_type": task_type}}))
        prompt = render_edit_skill(tmp_path)
        assert name in prompt
        assert f".harness/edit_skill/{name}/references" in prompt
        assert all(other not in prompt for other in EDIT_SKILLS.values() if other != name)
    contract.write_text(json.dumps({"schema_version": "edit-task-contract-v1", "chain_metadata": {"task_type": "Unknown"}}))
    assert render_edit_skill(tmp_path) == ""


def test_generate_skill_exposes_signatures_without_reference_body(tmp_path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1",
        "chain_metadata": {"task_type": "Real-time Dashboard", "edit_skill_stack": "react"},
    }))
    prompt = render_edit_skill(tmp_path)
    assert "mountRealtimeDashboard" in prompt
    assert "Use copy_from to reuse this immutable implementation" in prompt
    assert "body is intentionally not inlined" in prompt
    assert "const root = document.createElement(\"section\")" not in prompt


def test_repair_reuses_installed_skill_without_repeating_canonical_body(tmp_path):
    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1",
        "chain_metadata": {"task_type": "Shopping Cart", "edit_skill_stack": "vanilla"},
    }))
    render_edit_skill(tmp_path)
    for destination in reference_destinations(tmp_path).values():
        target = tmp_path / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// current locally adapted core\n")

    prompt = render_edit_skill(tmp_path, mode="repair")

    assert "Skill core is already installed" in prompt
    assert "Full immutable reference source:" not in prompt
    assert "Host integration example" in prompt


@pytest.mark.anyio
async def test_selected_reference_is_readable_by_actual_agent_tool(tmp_path):
    from src.agents.openai_tools import OpenAIToolExecutor

    (tmp_path / ".harness").mkdir()
    (tmp_path / ".harness/edit_task_contract.json").write_text(json.dumps({
        "schema_version": "edit-task-contract-v1", "chain_metadata": {"task_type": "Data Table"}
    }))
    render_edit_skill(tmp_path)
    executor = OpenAIToolExecutor(workdir=tmp_path, allow_bash=False)
    try:
        result = await executor.execute("read_file", {
            "path": ".harness/edit_skill/webcompass-data-table/references/table.js"
        })
        assert result.ok and "function mountDataTable" in result.output
        assert not (tmp_path / "frontend").exists()
    finally:
        await executor.close()


def test_cart_reference_transitions_and_invalid_updates():
    checks = """
const assert = require('node:assert/strict');
const data = [{id:'a',label:'Book A',unitPriceMinor:1999},
              {id:'b',label:'Book B',unitPriceMinor:305}];
const cart = createCart(data);
assert.equal(cart.totalMinor(),0);
cart.add('a'); cart.add('a'); cart.add('b');
assert.equal(cart.lines().length,2);
assert.equal(cart.totalMinor(),4303);
cart.setQuantity('a',3);
assert.equal(cart.totalMinor(),6302);
for (const invalid of [0,-1,1.5,NaN,Infinity,'3']) {
  assert.throws(()=>cart.setQuantity('a',invalid));
  assert.equal(cart.totalMinor(),6302);
}
assert.throws(()=>cart.add('missing'));
assert.throws(()=>cart.setQuantity('a',Number.MAX_SAFE_INTEGER));
assert.equal(cart.totalMinor(),6302);
data[0].unitPriceMinor=1;
cart.lines()[0].quantity=100;
assert.equal(cart.totalMinor(),6302);
assert.throws(()=>createCart([data[0],data[0]]));
assert.throws(()=>createCart([{id:'c',label:'X',unitPriceMinor:0.1}]));
assert.equal(createCart(data).totalMinor(),0);
"""
    subprocess.run(["node", "-e", (SKILL / "references/cart.js").read_text() + checks],
                   check=True, capture_output=True, text=True)




@pytest.mark.anyio
async def test_component_renders_updates_and_cleans_up_in_browser():
    from playwright.async_api import async_playwright
    from src.utils.playwright_browser import launch_chromium

    async with async_playwright() as playwright:
        browser = await launch_chromium(playwright, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('''<main><p id="host">Existing catalogue</p>
              <form id="catalogue"><button data-id="a"><span>Add A</span></button>
              <button data-id="b">Add B</button></form><div id="basket"></div></main>''')
            await page.add_script_tag(path=str(SKILL / "references/cart.js"))
            await page.add_style_tag(path=str(SKILL / "references/cart.css"))
            await page.evaluate('''() => {
              window.options = {
                items: [{id:'a',label:'Book A',unitPriceMinor:1999},
                        {id:'b',label:'Book <B>',unitPriceMinor:305}],
                container:document.querySelector('#basket'),
                addRoot:document.querySelector('#catalogue'),addSelector:'button',
                getItemId:button=>button.dataset.id,
                formatMoney:minor=>'$'+(minor/100).toFixed(2),
                initialQuantities:[{id:'b',quantity:2}]
              };
              window.basket = mountShoppingCart(options);
            }''')
            total = page.locator('[data-cart-total]')
            assert await total.text_content() == "$6.10"
            await page.locator('button[data-id=a] span').click()
            await page.locator('button[data-id=a]').click()
            assert await page.locator('[data-cart-id]').count() == 2
            assert await total.text_content() == "$46.08"
            quantity = page.locator('[data-cart-id=a] input')
            await quantity.evaluate('(node)=>window.originalQuantityNode=node')
            await quantity.fill('3')
            assert await total.text_content() == "$66.07"
            assert await page.locator('[data-cart-id=a] [data-cart-line-total]').text_content() == "$59.97"
            assert await quantity.evaluate('(node)=>node===window.originalQuantityNode')
            assert await quantity.evaluate('(node)=>node===document.activeElement')
            await quantity.fill('0')
            await quantity.press('Tab')
            assert await quantity.input_value() == '3'
            assert await total.text_content() == "$66.07"
            assert await page.locator('[data-cart-id=b] .wc-cart__item').text_content() == 'Book <B>'
            assert await page.locator('[data-cart-id=b] b').count() == 0
            assert await page.evaluate('''() => {
              try { mountShoppingCart(options); return false; }
              catch (error) { return error.message === 'Cart is already mounted'; }
            }''')
            await page.evaluate('''() => {
              basket.snapshot().lines[0].quantity=999;
              if (basket.snapshot().totalMinor!==6607) throw Error('snapshot mutated state');
              basket.destroy(); basket.destroy();
              window.basket = mountShoppingCart({...options,initialQuantities:[]});
            }''')
            await page.locator('button[data-id=a]').click()
            assert await total.text_content() == "$19.99"
            assert await page.locator('[data-cart-id]').count() == 1
            assert await page.locator('#host').text_content() == 'Existing catalogue'
        finally:
            await browser.close()
