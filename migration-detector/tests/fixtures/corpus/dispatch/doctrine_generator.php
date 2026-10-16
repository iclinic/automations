<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * Este arquivo **não é uma migração**: é o gerador que escreve uma. O
 * consumidor do stack Doctrine tem um comando `make` que monta o arquivo novo a
 * partir de um template, e o template mora num heredoc interpolado — o que
 * significa que este arquivo contém, escritos por extenso,
 * `use Doctrine\Migrations\AbstractMigration`, `extends AbstractMigration` e
 * `public function up(Schema $schema): void`.
 *
 * Um marcador de dispatch por substring — como o `"MigrationInterface" in
 * source` do TypeORM, que pode ser um porque nenhum arquivo daquele consumidor
 * gera TypeScript — leria isto como migração. O parser abriria o `up()` do
 * template, não acharia `addSql` nenhum, e o dispatch publicaria um `unknown`
 * em todo PR que mexesse no gerador.
 *
 * O marcador do Doctrine roda sobre o texto que o scanner do parser já apagou,
 * então dentro do heredoc não há código: o marcador vê exatamente o que o parser
 * vê. Este arquivo cai no `unknown` de "casou `migration_paths` e não é uma
 * migração de doctrine" — que é a resposta honesta, e não a classificação de um
 * template.
 */

declare(strict_types=1);

namespace Migrations\Migrator\Command;

class MakeCommand
{
    private string $baseDir;

    public function __construct(string $baseDir)
    {
        $this->baseDir = $baseDir;
    }

    public function execute(?string $name, ?string $database): void
    {
        $className = 'Version' . date('YmdHis');
        $target = $this->baseDir . '/Migrations/' . $database . '/' . $className . '.php';

        file_put_contents($target, $this->generateTemplate($className, (string) $name));
    }

    private function generateTemplate(string $className, string $name): string
    {
        return <<<PHP
<?php

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

/**
 * Migration: {$name}
 */
final class {$className} extends AbstractMigration
{
    public function getDescription(): string
    {
        return '{$name}';
    }

    public function up(Schema \$schema): void
    {
        // TODO: implementar as alteracoes
    }

    public function down(Schema \$schema): void
    {
        // TODO: implementar o rollback
    }
}
PHP;
    }
}
