# Fluxo de trabalho do HTS Bot Multi servers

- O proprietário autorizou commits e pushes diretamente na `main` para este bot. Esse é o fluxo padrão após concluir e validar as alterações; não é necessário abrir uma branch ou pedir nova confirmação para publicar no GitHub.
- Antes do push, atualize as referências remotas e preserve alterações concorrentes. Não use force push para contornar divergências.
- Preserve credenciais, dados e personalizações locais, inclusive alterações do usuário no `.env.example`. Publique apenas os valores portáveis do template.
- Publicar no GitHub não autoriza implantação ou reinício de serviços no dedicado. Execute essas ações apenas quando solicitadas.
- Preserve o isolamento entre comunidades, as autorizações de vínculo e os limites de leitura/concorrência ao alterar funções multisservidores.
