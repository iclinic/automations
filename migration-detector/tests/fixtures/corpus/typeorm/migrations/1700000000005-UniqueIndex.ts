import {MigrationInterface, QueryRunner} from "typeorm";

export class UniqueIndex1700000000005 implements MigrationInterface {

    public async up(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`CREATE UNIQUE INDEX "UQ_schedule_room" ON "schedule" ("room")`);
        await queryRunner.query(`DROP INDEX "IDX_schedule_room"`);
    }

    public async down(queryRunner: QueryRunner): Promise<any> {
        await queryRunner.query(`SELECT 1`);
    }

}
