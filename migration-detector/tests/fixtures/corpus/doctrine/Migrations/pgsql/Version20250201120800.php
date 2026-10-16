<?php

/**
 * Por que esta fixture existe
 * ---------------------------
 *
 * A migração escrita com o **schema builder** do Doctrine em vez de `addSql()`.
 * O parser só lê o SQL de `addSql`, e cada outra chamada de `up()` que não é
 * leitura sai `unknown` com o nome do método: `createTable`, `addColumn`,
 * `setPrimaryKey`. Antes ele devolvia lista vazia e quem respondia era o portão
 * do parser mudo em `detect/__init__.py` — que não dispara quando há um
 * `addSql` ao lado, e era por aí que um `dropTable` sumia da mensagem.
 */

declare(strict_types=1);

namespace Migrations;

use Doctrine\DBAL\Schema\Schema;
use Doctrine\Migrations\AbstractMigration;

final class Version20250201120800 extends AbstractMigration
{
    public function getDescription(): string
    {
        return 'Cria a tabela de posicoes pelo schema builder';
    }

    public function up(Schema $schema): void
    {
        $table = $schema->createTable('warehouse_bin');
        $table->addColumn('bin_label', 'string', ['length' => 40]);
        $table->setPrimaryKey(['bin_label']);
    }

    public function down(Schema $schema): void
    {
        $schema->dropTable('warehouse_bin');
    }
}
