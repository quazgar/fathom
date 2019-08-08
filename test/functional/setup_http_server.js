const http = require('http');
const fs = require('fs');
const url = require('url');

const PORT = 8000;

const server = http.createServer((request, response) => {
    const path = url.parse(request.url).pathname;
    fs.readFile(__dirname + path, 'utf8', (error, data) => {
        if (error) {
            response.writeHead(404);
            response.write(`There was an error: ${error.errno}, ${error.code}`);
            response.end();
        } else {
            response.writeHead(200, {'Content-Type': 'text/html'});
            response.write(data);
            response.end();
        }
    });
});
server.listen(PORT);
console.log(`Serving from ${__dirname} at http://localhost:${PORT}...`);
