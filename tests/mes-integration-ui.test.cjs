const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

for (const [file, loader] of [
	['material_request.js', 'load_mes_material_request_integration_enabled'],
	['stock_entry.js', 'load_mes_integration_enabled'],
]) {
	function setup() {
		const requests = [];
		const context = {
			document: {},
			$: () => ({ ready() {}, on() {} }),
			frappe: {
				ui: { form: { on() {} } },
				db: { get_value() {
					let resolve, reject;
					const pending = new Promise((ok, fail) => { resolve = ok; reject = fail; });
					requests.push({ resolve, reject });
					// A jQuery-style thenable does not provide Promise.prototype.finally.
					return { then: pending.then.bind(pending) };
				} },
			},
			cint: value => Number(value) || 0,
		};
		vm.createContext(context);
		let source = fs.readFileSync(path.join(__dirname, '../mes_integration/public/js', file), 'utf8');
		if (file === 'stock_entry.js') {
			source = source.replace(/\}\)\(\);\s*$/, `globalThis.${loader} = ${loader};\n})();`);
		}
		vm.runInContext(source, context);
		return { load: context[loader], requests };
	}

	test(`${file}: assimilates the framework thenable and shares an in-flight read`, async () => {
		const { load, requests } = setup();
		const frm = { doc: { company: 'Company A' } };
		const first = load(frm);
		assert.equal(load(frm), first);
		assert.equal(requests.length, 1);
		requests[0].resolve({ message: { custom_enable_mes_integration: 1 } });
		assert.equal(await first, true);
		assert.equal(frm._mes_integration_enabled, true);
		assert.equal(frm._mes_integration_enabled_promise, null);
	});

	test(`${file}: an old company response cannot overwrite the current company`, async () => {
		const { load, requests } = setup();
		const frm = { doc: { company: 'Company A' } };
		const first = load(frm);
		frm.doc.company = 'Company B';
		const second = load(frm);
		requests[0].resolve({ message: { custom_enable_mes_integration: 1 } });
		await first;
		assert.equal(frm._mes_integration_enabled, false);
		assert.equal(frm._mes_integration_enabled_promise, second);
		requests[1].resolve({ message: { custom_enable_mes_integration: 0 } });
		assert.equal(await second, false);
	});

	test(`${file}: a failed lookup keeps integration disabled and clears pending state`, async () => {
		const { load, requests } = setup();
		const frm = { doc: { company: 'Company A' } };
		const pending = load(frm);
		requests[0].reject(new Error('lookup failed'));
		assert.equal(await pending, false);
		assert.equal(frm._mes_integration_enabled, false);
		assert.equal(frm._mes_integration_enabled_promise, null);
	});
}
