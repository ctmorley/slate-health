const puppeteer = require('puppeteer');
const path = require('path');

(async () => {
    const browser = await puppeteer.launch({ headless: 'new' });
    const page = await browser.newPage();

    const filePath = 'file://' + path.resolve(__dirname, 'proposal.html');
    await page.goto(filePath, { waitUntil: 'networkidle0', timeout: 30000 });

    // Wait for Chart.js canvases to render
    await page.waitForFunction(() => {
        const canvases = document.querySelectorAll('canvas');
        return canvases.length > 0 && canvases[0].getContext('2d').__currentTransform !== undefined;
    }, { timeout: 5000 }).catch(() => {});

    // Extra wait for all charts to finish drawing
    await new Promise(r => setTimeout(r, 2000));

    await page.pdf({
        path: path.resolve(__dirname, 'Slate_Health_Business_Proposal.pdf'),
        format: 'A4',
        printBackground: true,
        margin: { top: 0, right: 0, bottom: 0, left: 0 },
    });

    await browser.close();
    console.log('PDF generated successfully');
})();
