import {
    createProductActionButton,
    createProductElement,
} from './product-ui.mjs';
import { createIcon } from './icons.mjs';

export function createOnboardingController(options) {
    const {
        documentRef,
        getStorage,
        storageKey,
        showModal,
        hideModal,
        showToast,
        showProviderSettings,
    } = options;
    const element = (tag, className = '', text = null) => (
        createProductElement(documentRef, tag, className, text)
    );
    const actionButton = (label, className = 'secondary-btn', iconName = null) => (
        createProductActionButton(documentRef, label, className, iconName)
    );

    function seen() {
        try { return getStorage().getItem(storageKey) === 'complete'; }
        catch (_error) { return false; }
    }

    function complete() {
        try { getStorage().setItem(storageKey, 'complete'); }
        catch (_error) {}
    }

    function show() {
        if (seen()) return false;
        const body = element('div', 'onboarding product-tool');
        body.appendChild(element('p', 'onboarding-lead', '选择模型来源前，先确认数据会在哪里处理。你之后可以在顶部“模型来源”随时切换。'));
        const optionsRoot = element('div', 'onboarding-options');
        const local = element('section', 'onboarding-card recommended');
        local.append(
            createIcon(documentRef, 'provider', { size: 24 }),
            element('h3', '', '本地 Ollama'),
            element('p', '', '模型请求在本机 Ollama 中处理。未配置云端来源时，不会把对话发送给云端模型。'),
        );
        const chooseLocal = actionButton('使用本地模型', 'primary-btn', 'arrowRight');
        local.appendChild(chooseLocal);
        const cloud = element('section', 'onboarding-card');
        cloud.append(
            createIcon(documentRef, 'cloud', { size: 24 }),
            element('h3', '', '云端 API'),
            element('p', '', '调用云端时，当前请求所需的角色卡、世界书、剧情摘要与对话历史会发送给你配置的服务商。'),
        );
        const chooseCloud = actionButton('配置云端 API', 'secondary-btn', 'settings');
        cloud.appendChild(chooseCloud);
        optionsRoot.append(local, cloud);
        body.appendChild(optionsRoot);
        body.appendChild(element('p', 'product-privacy-note', 'API Key 只保存在本机凭据存储中，不会出现在诊断中心或脱敏支持包里。关闭此窗口不会记为完成，下次启动仍会提示。'));
        chooseLocal.addEventListener('click', () => {
            complete();
            hideModal();
            showToast('已选择本地优先；可在顶部“模型来源”切换');
        });
        chooseCloud.addEventListener('click', async () => {
            let opened = false;
            chooseCloud.disabled = true;
            chooseCloud.setAttribute('aria-busy', 'true');
            chooseLocal.disabled = true;
            try {
                opened = await showProviderSettings() === true;
                if (opened) complete();
                else showToast('模型来源设置未打开，请检查提示后重试');
            } catch (_error) {
                showToast('模型来源设置未能打开，请稍后重试');
            } finally {
                chooseCloud.disabled = false;
                chooseCloud.setAttribute('aria-busy', 'false');
                chooseLocal.disabled = false;
                if (!opened && chooseCloud.isConnected) chooseCloud.focus();
            }
        });
        showModal({ title: '欢迎使用本地酒馆', body });
        return true;
    }

    return Object.freeze({ seen, show });
}
