document.addEventListener('DOMContentLoaded', () => {
    // Elements
    const btnUpdater = document.getElementById('btn-download-updater');
    const btnManual = document.getElementById('btn-download-manual');

    const valChineseRelease = document.getElementById('val-chinese-release');
    const valRawVersion = document.getElementById('val-raw-version');
    const valVersion = document.getElementById('val-version');
    const valNotes = document.getElementById('val-notes');

    // Fetch latest.json (production version)
    // We try to load the actual latest.json. If it fails, we assume not published yet.
    fetch('latest.json')
        .then(response => {
            if (!response.ok) {
                throw new Error('清单未发布');
            }
            return response.json();
        })
        .then(data => {
            // Validate schema and data format
            if (data.schema_version !== 1) throw new Error('无效的 schema_version');
            if (!data.version || typeof data.version !== 'string') throw new Error('无效的 version');

            // Validate updater
            const updater = data.updater;
            if (!updater || typeof updater.size !== 'number' || updater.size <= 0 || !/^[a-f0-9]{64}$/i.test(updater.sha256)) {
                throw new Error('无效的 updater 信息');
            }

            // Validate package
            const pkg = data.package;
            if (!pkg || typeof pkg.size !== 'number' || pkg.size <= 0 || !/^[a-f0-9]{64}$/i.test(pkg.sha256)) {
                throw new Error('无效的 package 信息');
            }

            // Function to validate URL is HTTPS and has no credentials
            const validateUrl = (urlStr) => {
                try {
                    const url = new URL(urlStr);
                    return url.protocol === 'https:' && !url.username && !url.password;
                } catch {
                    return false;
                }
            };

            if (!validateUrl(updater.url)) throw new Error('更新器 URL 无效或不安全');
            if (!validateUrl(pkg.url)) throw new Error('语言包 URL 无效或不安全');

            // Update UI with real data
            if (data.source && data.source.chinese_release) {
                valChineseRelease.textContent = data.version; // or manifest.version if nested
            }
            if (data.source && data.source.raw_version) {
                const rawVer = data.source.raw_version || '';
                valRawVersion.title = rawVer;
                const winMatch = /^windows-sha256:([0-9a-fA-F]{8})[0-9a-fA-F]*$/.exec(rawVer);
                const cdnMatch = /^l(\d{4})(\d{2})(\d{2})_/.exec(rawVer);
                if (winMatch) {
                    valRawVersion.textContent = `Windows · ${winMatch[1].toLowerCase()}`;
                } else if (cdnMatch) {
                    valRawVersion.textContent = `${cdnMatch[1]}.${cdnMatch[2]}.${cdnMatch[3]}`;
                } else {
                    valRawVersion.textContent = rawVer;
                }
            }
            if (data.version) {
                valVersion.textContent = data.source.chinese_release;
            }
            if (data.notes) {
                valNotes.textContent = data.notes;
            }

            // Enable download buttons if URLs are provided
            if (data.updater && data.updater.url) {
                btnUpdater.disabled = false;
                btnUpdater.innerHTML = '<span class="icon">↓</span> 下载 Windows 更新器';
                btnUpdater.addEventListener('click', () => {
                    window.location.href = data.updater.url;
                });
            } else {
                btnUpdater.innerHTML = '<span class="icon">↓</span> 更新器不可用';
            }

            if (data.package && data.package.url) {
                btnManual.disabled = false;
                btnManual.addEventListener('click', () => {
                    window.location.href = data.package.url;
                });
            }
        })
        .catch(error => {
            console.warn('Cannot fetch or validate latest.json:', error);
            // Not published state
            valChineseRelease.textContent = '暂未发布';
            valRawVersion.textContent = '暂未发布';
            valVersion.textContent = '暂未发布';
            valNotes.textContent = '等待正式上线...';

            btnUpdater.disabled = true;
            btnUpdater.innerHTML = '<span class="icon">↓</span> 暂未发布';

            btnManual.disabled = true;
            btnManual.textContent = '敬请期待 >';
        });
});