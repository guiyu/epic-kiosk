const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/index.html'), 'utf8');
const source = html.slice(html.indexOf('async function doQuery()'), html.indexOf('{% endraw %}'));

async function check(method) {
    const requests = [], messages = [], elements = {};
    const context = vm.createContext({
        accountAuthMethod: method, accountAccessToken: 'local-session', activeTask: null,
        document: {getElementById: id => elements[id] ||= {classList: {add() {}, remove() {}, toggle() {}}}},
        axios: {
            get: async url => { requests.push(url); throw {response: {status: 401}}; },
            post: async url => { requests.push(url); return {data: {status: 'queued', task_id: 'task', access_token: 'task-session'}}; }
        },
        localStorage: {getItem: () => JSON.stringify({email: 'fixture@example.com'}), removeItem() {}},
        sessionStorage: {setItem() {}},
        showToast: message => messages.push(message), switchTab() {}, setNativeBusy() {}, startNativeTaskPolling() {}
    });
    vm.runInContext(source, context);
    if (method === 'password') {
        await vm.runInContext('doQuery()', context);
        assert.equal(context.accountAccessToken, 'local-session');
        await vm.runInContext('claimCurrentAccount()', context);
        assert.deepEqual(requests, []);
        assert.equal(context.activeTask, null);
        assert(messages.some(message => message.includes('官方登录')));
    } else {
        await vm.runInContext('claimCurrentAccount()', context);
        assert.deepEqual(requests, ['/api/claim']);
        assert.equal(context.activeTask.email, 'fixture@example.com');
        assert.equal(context.activeTask.method, 'epic_oauth');
    }
}
(async () => { await check('password'); await check('epic_oauth'); console.log('Password-session guard and native claim tracking passed'); })().catch(error => { console.error(error); process.exitCode = 1; });
